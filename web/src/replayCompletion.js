/** Mechanical replay completion: provider boundaries alone cannot delete audio. */
export function createReplayCompletion() {
  let transition = false, turnComplete = false, epoch = 0;
  return {
    event(event) {
      if (event.type === 'capability_transition_requested') {
        transition = true; turnComplete = false; ++epoch;
      } else if (event.type === 'capability_ready') {
        transition = false; turnComplete = false; ++epoch;
      } else if (event.type === 'turn_complete') {
        turnComplete = true;
      }
    },
    get epoch() { return epoch; },
    get canReadDisposition() { return turnComplete && !transition; },
    confirmed(status, readEpoch) {
      return readEpoch === epoch && turnComplete && !transition
        && ['archived', 'ephemeral_processed'].includes(status);
    },
  };
}
