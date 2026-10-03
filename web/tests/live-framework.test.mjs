import test from "node:test";
import assert from "node:assert/strict";
import { createLiveAudioSender } from "@onedayonemasterpiece/live-interaction/browser";

test("steady-state speech above the former 48 kB watermark stays alive", () => {
  let error;
  let sends = 0;
  const sender = createLiveAudioSender({
    send: () => {
      sends += 1;
      return new Promise(() => {});
    },
    onError: value => {
      error = value;
    },
  });

  for (let index = 0; index < 18; index += 1) {
    sender.push(new Int16Array(1486).fill(1000), 0.05);
  }

  const stats = sender.stats();
  assert.equal(sends, 1);
  assert.ok(stats.queued_pcm_bytes > 48_000, stats);
  assert.ok(stats.queued_pcm_bytes < 80_000, stats);
  assert.equal(error, undefined);
  sender.stop();
});