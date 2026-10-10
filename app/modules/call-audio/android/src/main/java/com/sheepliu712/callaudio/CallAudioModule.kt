package com.sheepliu712.callaudio

import android.Manifest
import android.app.Activity
import android.app.Application
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.os.Bundle
import android.util.Base64
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

private const val CAPTURE_RATE = 16_000
private const val PLAYBACK_RATE = 24_000
private const val CHANNELS = 1
private const val MAX_CAPTURE_BUFFER_BYTES = CAPTURE_RATE * 2 * 3
private const val MAX_PLAYBACK_BUFFER_BYTES = PLAYBACK_RATE * 2 * 4

class CallAudioModule : Module(), Application.ActivityLifecycleCallbacks {
  private val captureRunning = AtomicBoolean(false)
  private val playbackRunning = AtomicBoolean(false)
  private val generation = AtomicLong(0)
  private val streamGenerations = ConcurrentHashMap<StreamKey, Long>()
  private val tombstonedStreams = ConcurrentHashMap.newKeySet<StreamKey>()
  private val queue = BoundedByteQueue(MAX_PLAYBACK_BUFFER_BYTES)
  private var captureThread: Thread? = null
  private var playbackThread: Thread? = null
  private var sessionReady = false
  private var audioRecord: AudioRecord? = null
  private var audioTrack: AudioTrack? = null
  private var echoCanceler: AcousticEchoCanceler? = null
  private var originalAudioMode: Int? = null
  private var routeCallback: AudioDeviceCallback? = null
  private var playbackHeadBase = 0L
  private var resetEpoch = 0L
  @Volatile private var pendingFlush = false
  private val stoppedReceiptLock = java.lang.Object()
  private val stoppedReceipts = ArrayDeque<Pair<StreamKey, Long>>()
  private val pendingFinalsLock = java.lang.Object()
  private val pendingFinals = ArrayDeque<PlaybackChunk>()
  private var resumedActivities = 0
  private var lifecycleRegistered = false

  override fun definition() = ModuleDefinition {
    Name("CallAudio")
    Events(
      "onCapturedAudio",
      "onPlaybackCompleted",
      "onPlaybackStopped",
      "onAudioFailure",
      "onLifecycle",
      "onRouteChanged",
    )

    AsyncFunction("getCapability") { probeCapability() }
    AsyncFunction("startSession") { startSession() }
    AsyncFunction("stopSession") { closeSession() }
    AsyncFunction("startCapture") { startCapture() }
    AsyncFunction("stopCapture") { stopCapture() }
    AsyncFunction("enqueuePlayback") {
        responseId: String,
        streamId: Int,
        payloadBase64: String,
        isFinal: Boolean,
      ->
      enqueuePlayback(responseId, streamId, payloadBase64, isFinal)
    }
    AsyncFunction("stopPlayback") { responseId: String?, streamId: Int? ->
      stopPlayback(responseId, streamId)
    }
    AsyncFunction("tombstone") { responseId: String, streamId: Int ->
      tombstone(StreamKey(responseId, streamId), true)
    }

    OnDestroy { closeSession() }
  }

  private fun startSession(): Map<String, Any?> {
    val context = requireContext()
    if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
      return capability(false, "permission_denied")
    }
    val captureProbeError = probeCaptureInitialization()
    if (captureProbeError != null) return capability(false, captureProbeError)
    registerLifecycle(context)
    val manager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    if (originalAudioMode == null) originalAudioMode = manager.mode
    manager.mode = AudioManager.MODE_IN_COMMUNICATION
    registerRouteCallback(manager)
    try {
      startPlayback()
    } catch (error: IllegalStateException) {
      restoreAudioMode()
      unregisterLifecycle()
      return capability(false, error.message ?: "playback_initialization_failed")
    }
    sessionReady = true
    return capability(true, null)
  }

  private fun probeCapability(): Map<String, Any?> {
    val context = requireContext()
    if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
      return capability(false, "permission_denied")
    }
    if (sessionReady) return capability(true, null)
    val captureMin = AudioRecord.getMinBufferSize(
      CAPTURE_RATE,
      AudioFormat.CHANNEL_IN_MONO,
      AudioFormat.ENCODING_PCM_16BIT,
    )
    val playbackMin = AudioTrack.getMinBufferSize(
      PLAYBACK_RATE,
      AudioFormat.CHANNEL_OUT_MONO,
      AudioFormat.ENCODING_PCM_16BIT,
    )
    if (captureMin <= 0 || playbackMin <= 0) return capability(false, "required_format_unavailable")
    return capability(false, "not_initialized")
  }

  private fun startCapture(): Map<String, Any?> {
    val context = requireContext()
    if (!sessionReady) return capability(false, "session_not_initialized")
    if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
      return capability(false, "permission_denied")
    }
    if (captureRunning.get()) return capability(true, null)

    val minBytes = AudioRecord.getMinBufferSize(
      CAPTURE_RATE,
      AudioFormat.CHANNEL_IN_MONO,
      AudioFormat.ENCODING_PCM_16BIT,
    )
    if (minBytes <= 0 || minBytes > MAX_CAPTURE_BUFFER_BYTES) {
      return capability(false, "capture_format_unavailable")
    }
    val record = try {
      AudioRecord(
        MediaRecorder.AudioSource.VOICE_COMMUNICATION,
        CAPTURE_RATE,
        AudioFormat.CHANNEL_IN_MONO,
        AudioFormat.ENCODING_PCM_16BIT,
        minOf(MAX_CAPTURE_BUFFER_BYTES, maxOf(minBytes * 2, 3200)),
      )
    } catch (_: IllegalArgumentException) {
      return capability(false, "capture_initialization_failed")
    } catch (_: SecurityException) {
      return capability(false, "permission_denied")
    }
    if (record.state != AudioRecord.STATE_INITIALIZED || record.sampleRate != CAPTURE_RATE) {
      record.release()
      return capability(false, "capture_initialization_failed")
    }
    audioRecord = record
    val aecResult = enableAec(record.audioSessionId)
    try {
      record.startRecording()
    } catch (_: IllegalStateException) {
      releaseCapture()
      return capability(false, "capture_start_failed")
    } catch (_: SecurityException) {
      releaseCapture()
      return capability(false, "permission_denied")
    }
    if (record.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
      releaseCapture()
      return capability(false, "capture_start_failed")
    }
    captureRunning.set(true)
    captureThread = Thread({ captureLoop(record) }, "CallAudioCapture").apply { start() }
    return capability(true, null, aecResult)
  }

  private fun probeCaptureInitialization(): String? {
    val minBytes = AudioRecord.getMinBufferSize(
      CAPTURE_RATE,
      AudioFormat.CHANNEL_IN_MONO,
      AudioFormat.ENCODING_PCM_16BIT,
    )
    if (minBytes <= 0 || minBytes > MAX_CAPTURE_BUFFER_BYTES) return "capture_format_unavailable"
    val record = try {
      AudioRecord(
        MediaRecorder.AudioSource.VOICE_COMMUNICATION,
        CAPTURE_RATE,
        AudioFormat.CHANNEL_IN_MONO,
        AudioFormat.ENCODING_PCM_16BIT,
        minOf(MAX_CAPTURE_BUFFER_BYTES, maxOf(minBytes * 2, 3200)),
      )
    } catch (_: IllegalArgumentException) {
      return "capture_initialization_failed"
    } catch (_: SecurityException) {
      return "permission_denied"
    }
    val error = if (record.state == AudioRecord.STATE_INITIALIZED && record.sampleRate == CAPTURE_RATE) {
      null
    } else {
      "capture_initialization_failed"
    }
    record.release()
    return error
  }

  private fun captureLoop(record: AudioRecord) {
    val buffer = ByteArray(3200)
    var sequence = 1L
    while (captureRunning.get()) {
      val read = record.read(buffer, 0, buffer.size, AudioRecord.READ_BLOCKING)
      if (read > 0) {
        sendEvent(
          "onCapturedAudio",
          mapOf(
            "sequence" to sequence++,
            "payloadBase64" to Base64.encodeToString(buffer, 0, read, Base64.NO_WRAP),
            "format" to format(CAPTURE_RATE),
          ),
        )
      } else if (read < 0 && captureRunning.get()) {
        failure("capture_read_failed", "capture")
        captureRunning.set(false)
      }
    }
  }

  private fun stopCapture() {
    captureRunning.set(false)
    runCatching { audioRecord?.stop() }
    captureThread?.interrupt()
    captureThread?.join(250)
    captureThread = null
    releaseCapture()
  }

  private fun releaseCapture() {
    echoCanceler?.release()
    echoCanceler = null
    audioRecord?.release()
    audioRecord = null
  }

  private fun startPlayback() {
    if (playbackRunning.get()) return
    val minBytes = AudioTrack.getMinBufferSize(
      PLAYBACK_RATE,
      AudioFormat.CHANNEL_OUT_MONO,
      AudioFormat.ENCODING_PCM_16BIT,
    )
    if (minBytes <= 0) throw IllegalStateException("playback_format_unavailable")
    val track = AudioTrack.Builder()
      .setAudioAttributes(
        AudioAttributes.Builder()
          .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
          .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
          .build(),
      )
      .setAudioFormat(
        AudioFormat.Builder()
          .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
          .setSampleRate(PLAYBACK_RATE)
          .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
          .build(),
      )
      .setBufferSizeInBytes(maxOf(minBytes * 2, 4800))
      .setTransferMode(AudioTrack.MODE_STREAM)
      .build()
    if (track.state != AudioTrack.STATE_INITIALIZED || track.sampleRate != PLAYBACK_RATE) {
      track.release()
      throw IllegalStateException("playback_initialization_failed")
    }
    audioTrack = track
    track.play()
    playbackRunning.set(true)
    playbackThread = Thread({ playbackLoop(track) }, "CallAudioPlayback").apply { start() }
  }

  private fun enqueuePlayback(
    responseId: String,
    streamId: Int,
    payloadBase64: String,
    isFinal: Boolean,
  ): Long {
    val payload = try {
      Base64.decode(payloadBase64, Base64.DEFAULT)
    } catch (_: IllegalArgumentException) {
      throw IllegalArgumentException("invalid_base64")
    }
    require(payload.isNotEmpty() && payload.size % 2 == 0) { "invalid_pcm_payload" }
    val key = StreamKey(responseId, streamId)
    check(!tombstonedStreams.contains(key)) { "playback_stream_tombstoned" }
    val currentGeneration = streamGenerations.computeIfAbsent(key) { generation.incrementAndGet() }
    check(queue.offer(PlaybackChunk(key, currentGeneration, payload, isFinal))) {
      "playback_buffer_full"
    }
    return currentGeneration
  }

  private fun playbackLoop(track: AudioTrack) {
    var previousHead = track.playbackHeadPosition.toLong() and 0xffffffffL
    var observedResetEpoch = resetEpoch
    var submittedFrames = 0L
    val finalTargets = mutableListOf<Pair<PlaybackChunk, Long>>()
    while (playbackRunning.get()) {
      if (observedResetEpoch != resetEpoch) {
        observedResetEpoch = resetEpoch
        previousHead = track.playbackHeadPosition.toLong() and 0xffffffffL
        submittedFrames = 0L
        finalTargets.clear()
      }
      synchronized(pendingFinalsLock) {
        while (pendingFinals.isNotEmpty()) {
          val finalChunk = pendingFinals.removeFirst()
          finalTargets.add(finalChunk to submittedFrames)
        }
      }
      if (pendingFlush) {
        track.pause()
        track.flush()
        playbackHeadBase = 0L
        previousHead = track.playbackHeadPosition.toLong() and 0xffffffffL
        submittedFrames = 0L
        finalTargets.clear()
        pendingFlush = false
        if (playbackRunning.get()) track.play()
        emitStoppedReceipts()
      }
      val chunk = if (finalTargets.isEmpty()) {
        queue.pollWhile { playbackRunning.get() }
      } else {
        Thread.sleep(5)
        null
      }
      if (chunk != null && isCurrent(chunk)) {
        var offset = 0
        while (offset < chunk.payload.size && playbackRunning.get() && isCurrent(chunk)) {
          val written = track.write(chunk.payload, offset, chunk.payload.size - offset, AudioTrack.WRITE_BLOCKING)
          if (written <= 0) {
            failure("playback_write_failed", "playback")
            break
          }
          offset += written
          submittedFrames += written / 2L
        }
      if (offset == chunk.payload.size && chunk.isFinal && isCurrent(chunk)) {
          synchronized(pendingFinalsLock) { pendingFinals.addLast(chunk) }
      }
      }

      val currentHead = track.playbackHeadPosition.toLong() and 0xffffffffL
      playbackHeadBase += PlaybackAccounting.unsignedDelta(previousHead, currentHead)
      previousHead = currentHead
      val iterator = finalTargets.iterator()
      while (iterator.hasNext()) {
        val (finalChunk, target) = iterator.next()
        if (!isCurrent(finalChunk)) {
          iterator.remove()
        } else if (playbackHeadBase >= target) {
          completion(finalChunk)
          iterator.remove()
        }
      }
    }
  }

  private fun completion(chunk: PlaybackChunk) {
    sendEvent(
      "onPlaybackCompleted",
      mapOf(
        "responseId" to chunk.key.responseId,
        "streamId" to chunk.key.streamId,
        "generation" to chunk.generation,
      ),
    )
  }

  private fun stopPlayback(responseId: String?, streamId: Int?) {
    if (responseId != null && streamId != null) {
      tombstone(StreamKey(responseId, streamId), true)
      return
    }
    val keys = streamGenerations.keys.toList()
    keys.forEach { tombstone(it, true) }
    queue.clear()
    flushPlaybackTrack()
  }

  private fun tombstone(key: StreamKey, emitReceipt: Boolean) {
    val stoppedGeneration = generation.incrementAndGet()
    tombstonedStreams.add(key)
    streamGenerations[key] = stoppedGeneration
    queue.remove(key)
    synchronized(pendingFinalsLock) {
      val retained = pendingFinals.filterNot { it.key == key }
      pendingFinals.clear()
      pendingFinals.addAll(retained)
    }
    flushPlaybackTrack()
    if (emitReceipt) synchronized(stoppedReceiptLock) {
      stoppedReceipts.addLast(key to stoppedGeneration)
    }
  }

  private fun isCurrent(chunk: PlaybackChunk): Boolean =
    streamGenerations[chunk.key] == chunk.generation

  private fun flushPlaybackTrack() {
    resetEpoch += 1
    pendingFlush = true
    queue.wake()
  }

  private fun emitStoppedReceipts() {
    val receipts = synchronized(stoppedReceiptLock) {
      stoppedReceipts.toList().also { stoppedReceipts.clear() }
    }
    receipts.forEach { (key, stoppedGeneration) ->
      sendEvent(
        "onPlaybackStopped",
        mapOf(
          "responseId" to key.responseId,
          "streamId" to key.streamId,
          "generation" to stoppedGeneration,
        ),
      )
    }
  }

  private fun closeSession() {
    stopCapture()
    playbackRunning.set(false)
    queue.clear()
    playbackThread?.interrupt()
    playbackThread?.join(250)
    playbackThread = null
    runCatching { audioTrack?.stop() }
    audioTrack?.release()
    audioTrack = null
    unregisterRouteCallback()
    restoreAudioMode()
    unregisterLifecycle()
    sessionReady = false
  }

  private fun capability(
    available: Boolean,
    reason: String?,
    aecResult: Pair<Boolean, String?> = false to null,
  ): Map<String, Any?> = mapOf(
    "available" to available,
    "reason" to reason,
    "captureFormat" to if (available) format(CAPTURE_RATE) else null,
    "playbackFormat" to if (available) format(PLAYBACK_RATE) else null,
    "aec" to mapOf(
      "available" to AcousticEchoCanceler.isAvailable(),
      "enabled" to aecResult.first,
      "error" to aecResult.second,
    ),
  )

  private fun enableAec(audioSessionId: Int): Pair<Boolean, String?> {
    if (!AcousticEchoCanceler.isAvailable()) return false to "aec_unavailable"
    val effect = AcousticEchoCanceler.create(audioSessionId)
      ?: return false to "aec_create_failed"
    echoCanceler = effect
    val result = effect.setEnabled(true)
    return if (result == 0 && effect.enabled) true to null else false to "aec_enable_failed"
  }

  private fun format(sampleRate: Int): Map<String, Any> = mapOf(
    "encoding" to "pcm_s16le",
    "sampleRateHz" to sampleRate,
    "channels" to CHANNELS,
  )

  private fun failure(code: String, operation: String) {
    sendEvent("onAudioFailure", mapOf("code" to code, "operation" to operation))
  }

  private fun requireContext(): Context =
    appContext.reactContext ?: throw IllegalStateException("react_context_unavailable")

  private fun registerLifecycle(context: Context) {
    if (lifecycleRegistered) return
    (context.applicationContext as Application).registerActivityLifecycleCallbacks(this)
    lifecycleRegistered = true
  }

  private fun unregisterLifecycle() {
    if (!lifecycleRegistered) return
    val context = appContext.reactContext ?: return
    (context.applicationContext as Application).unregisterActivityLifecycleCallbacks(this)
    lifecycleRegistered = false
  }

  private fun registerRouteCallback(manager: AudioManager) {
    if (routeCallback != null) return
    routeCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>) = emitRoutes(manager)
        override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>) = emitRoutes(manager)
      }
    manager.registerAudioDeviceCallback(routeCallback, null)
    emitRoutes(manager)
  }

  private fun unregisterRouteCallback() {
    val callback = routeCallback ?: return
    val context = appContext.reactContext ?: return
    (context.getSystemService(Context.AUDIO_SERVICE) as AudioManager).unregisterAudioDeviceCallback(callback)
    routeCallback = null
  }

  private fun emitRoutes(manager: AudioManager) {
    val outputs = manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS).map { deviceType(it.type) }
    sendEvent("onRouteChanged", mapOf("outputs" to outputs))
  }

  private fun deviceType(type: Int): String = when (type) {
    AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "bluetooth_a2dp"
    AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "bluetooth_sco"
    AudioDeviceInfo.TYPE_WIRED_HEADPHONES, AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired_headset"
    AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "earpiece"
    AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "speaker"
    else -> "other"
  }

  private fun restoreAudioMode() {
    val context = appContext.reactContext ?: return
    val previous = originalAudioMode ?: return
    (context.getSystemService(Context.AUDIO_SERVICE) as AudioManager).mode = previous
    originalAudioMode = null
  }

  override fun onActivityResumed(activity: Activity) { resumedActivities += 1 }
  override fun onActivityPaused(activity: Activity) {
    resumedActivities = maxOf(0, resumedActivities - 1)
    if (resumedActivities == 0) sendEvent("onLifecycle", mapOf("state" to "background"))
  }
  override fun onActivityCreated(activity: Activity, state: Bundle?) = Unit
  override fun onActivityStarted(activity: Activity) = Unit
  override fun onActivityStopped(activity: Activity) = Unit
  override fun onActivitySaveInstanceState(activity: Activity, state: Bundle) = Unit
  override fun onActivityDestroyed(activity: Activity) = Unit
}
