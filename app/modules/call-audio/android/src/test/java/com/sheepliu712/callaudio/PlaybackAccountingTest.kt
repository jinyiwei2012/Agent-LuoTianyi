package com.sheepliu712.callaudio

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PlaybackAccountingTest {
  @Test
  fun unsignedDeltaHandlesPlaybackHeadRollover() {
    assertEquals(5L, PlaybackAccounting.unsignedDelta(0xffff_fffeL, 3L))
  }

  @Test
  fun boundedQueueRejectsOverflowAndReleasesCapacity() {
    val queue = BoundedByteQueue(4)
    val key = StreamKey("response", 1)
    assertTrue(queue.offer(PlaybackChunk(key, 1, byteArrayOf(1, 2), false)))
    assertFalse(queue.offer(PlaybackChunk(key, 1, byteArrayOf(3, 4, 5), false)))
    assertEquals(2, queue.pollWhile { true }?.payload?.size)
    assertTrue(queue.offer(PlaybackChunk(key, 1, byteArrayOf(3, 4, 5), true)))
  }

  @Test
  fun removeDropsOnlyMatchingStream() {
    val queue = BoundedByteQueue(8)
    val removed = StreamKey("removed", 1)
    val kept = StreamKey("kept", 2)
    queue.offer(PlaybackChunk(removed, 1, byteArrayOf(1, 2), true))
    queue.offer(PlaybackChunk(kept, 1, byteArrayOf(3, 4), true))

    queue.remove(removed)

    assertEquals(kept, queue.pollWhile { true }?.key)
    assertNull(queue.pollWhile { false })
  }
}
