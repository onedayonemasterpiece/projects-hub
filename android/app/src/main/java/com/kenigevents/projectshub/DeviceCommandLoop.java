package com.kenigevents.projectshub;

import org.json.JSONObject;

import java.io.IOException;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

final class DeviceCommandLoop {
    interface Completion {
        void complete(String status, JSONObject result);
    }

    interface CommandHandler {
        void handle(ApiClient.ClaimedCommand command, Completion completion);
    }

    interface Listener {
        void onAuthenticationRejected();
    }

    private static final class Outcome {
        final String status;
        final JSONObject result;

        Outcome(String status, JSONObject result) {
            this.status = status;
            this.result = result;
        }
    }

    private final ApiClient api;
    private final String deviceToken;
    private final ExecutorService executor;
    private final CommandHandler handler;
    private final Listener listener;
    private final AtomicBoolean running = new AtomicBoolean(false);

    DeviceCommandLoop(
            ApiClient api,
            String deviceToken,
            ExecutorService executor,
            CommandHandler handler,
            Listener listener
    ) {
        this.api = api;
        this.deviceToken = deviceToken;
        this.executor = executor;
        this.handler = handler;
        this.listener = listener;
    }

    void start() {
        if (!running.compareAndSet(false, true)) return;
        executor.execute(this::runLoop);
    }

    void stop() {
        running.set(false);
    }

    private void runLoop() {
        while (running.get() && !Thread.currentThread().isInterrupted()) {
            try {
                ApiClient.ClaimedCommand command = api.nextCommand(deviceToken, 25000);
                if (command == null) continue;

                CountDownLatch completed = new CountDownLatch(1);
                AtomicReference<Outcome> outcome = new AtomicReference<>();
                AtomicBoolean accepted = new AtomicBoolean(false);

                handler.handle(command, (status, result) -> {
                    if (!accepted.compareAndSet(false, true)) return;
                    outcome.set(new Outcome(
                            status == null ? "failed" : status,
                            result == null ? new JSONObject() : result
                    ));
                    completed.countDown();
                });

                if (!completed.await(180, TimeUnit.SECONDS)) {
                    // Do not guess after a local capability timeout. The backend moves
                    // a claimed command to outcome_unknown rather than replaying it.
                    continue;
                }

                Outcome value = outcome.get();
                if (value != null) {
                    submitReceiptReliably(command, value);
                }
            } catch (ApiClient.ApiException apiFailure) {
                if (apiFailure.statusCode == 401) {
                    running.set(false);
                    listener.onAuthenticationRejected();
                    return;
                }
                sleepQuietly();
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                return;
            } catch (Exception ignored) {
                sleepQuietly();
            }
        }
    }

    private void submitReceiptReliably(
            ApiClient.ClaimedCommand command,
            Outcome value
    ) throws Exception {
        long backoffMs = 500;
        while (running.get() && !Thread.currentThread().isInterrupted()) {
            try {
                // Retrying the same terminal receipt is idempotent on the backend.
                // Never execute the already-applied calendar mutation again here.
                api.submitReceipt(deviceToken, command, value.status, value.result);
                return;
            } catch (ApiClient.ApiException failure) {
                if (failure.statusCode == 401) throw failure;
                if (failure.statusCode < 500
                        && failure.statusCode != 408
                        && failure.statusCode != 429) {
                    throw failure;
                }
            } catch (IOException transientFailure) {
                // Network failure after the local side effect: keep ownership of
                // this claimed command and retry only its terminal receipt.
            }
            Thread.sleep(backoffMs);
            backoffMs = Math.min(5000, backoffMs * 2);
        }
    }

    private void sleepQuietly() {
        if (!running.get()) return;
        try {
            Thread.sleep(1500);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }
}
