package com.sheepliu712.callaudio

internal object PlaybackAccounting {
  private const val UINT32_MODULUS = 1L shl 32

  fun unsignedDelta(previous: Long, current: Long): Long {
    require(previous in 0 until UINT32_MODULUS)
    require(current in 0 until UINT32_MODULUS)
    return if (current >= previous) current - previous else UINT32_MODULUS - previous + current
  }
}

internal class BoundedByteQueue(private val capacityBytes: Int) {
  private val entries = ArrayDeque<PlaybackChunk>()
  private var bytes = 0

  @Synchronized
  fun offer(chunk: PlaybackChunk): Boolean {
    if (chunk.payload.size > capacityBytes - bytes) return false
    entries.addLast(chunk)
    bytes += chunk.payload.size
    (this as java.lang.Object).notifyAll()
    return true
  }

  @Synchronized
  fun pollWhile(running: () -> Boolean): PlaybackChunk? {
    while (entries.isEmpty() && running()) {
      (this as java.lang.Object).wait(10)
    }
    if (entries.isEmpty()) return null
    return entries.removeFirst().also { bytes -= it.payload.size }
  }

  @Synchronized
  fun remove(identity: StreamKey) {
    val retained = entries.filterNot { it.key == identity }
    entries.clear()
    entries.addAll(retained)
    bytes = retained.sumOf { it.payload.size }
  }

  @Synchronized
  fun clear() {
    entries.clear()
    bytes = 0
    (this as java.lang.Object).notifyAll()
  }

  @Synchronized
  fun wake() {
    (this as java.lang.Object).notifyAll()
  }
}

internal data class StreamKey(val responseId: String, val streamId: Int)

internal data class PlaybackChunk(
  val key: StreamKey,
  val generation: Long,
  val payload: ByteArray,
  val isFinal: Boolean,
)
