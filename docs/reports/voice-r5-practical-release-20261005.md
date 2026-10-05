# PH-VOICE R5 — practical voice and release repair

Date: 2026-10-05. Product version: 0.1.32.
Continuation work: work_d8f68127a2a1e36bde3a2aca. No board/analytics branch changes.

## Restored actual baseline

The copied 0.1.29 report was behind actual main. Main and runtime were already 0.1.31 / 75ad4263f5ebeb36766c924231d947edc3a5305c; R4 onset admission and language hints were already delivered. The last public signed Android release was android-v31, versionName 0.1.27, published 2026-10-05T13:14:59Z. PWA code could update independently, but users were not receiving a corresponding fresh APK.

Do not re-run the completed R4 transcription hint experiment as a new audit. R4 measured a 60.001-second same-model turn, 600/600 audio ACKs, zero interim transcripts and first final transcript at 60.669 seconds. V03 genuine progressive transcription is still open. Two-second manual activityEnd is a semantic boundary, not a way to manufacture interim words.

## Reproduced faults and minimal changes

1. The signed APK workflow only watched android files and itself. Product-version bumps did not publish updated APKs. Watch src/projects_hub/version.py too; bind the release tag to the exact GITHUB_SHA.
2. The pre-transcript microphone status sat inside a chat container mounted only after some text existed. Mount it during an active voice session too. Display distinct admitted-speech and end/wait statuses; never insert generated placeholder words into a user transcript.
3. Shared client never returned from answering to listening after a completed reply. Restore listening only after provider completion and all scheduled/pending playback finishes, with a running mic and no pending input/resource pause.
4. WSS interruption during pending AudioContext.resume could resurrect cancelled audio. Separate playback generation now invalidates pending playback; Stop remains immediate. Resume is bounded and playback failures appear visibly instead of silently disappearing.
5. Manual sender finish did not schedule an activity_end if its PCM queue was already drained. Fix the pump and expose finishTurn() for the optional “Готово, отвечай” control. It queues a boundary behind accepted PCM without stopping the microphone/session or synthesizing a semantic instruction. Idle/noise/double click returns false. Queued does not mean acknowledged by the provider.

Shared changes are in live-interaction 0.3.23, PR #44, merge d9a34b3a568ce09b893c9fe3d36a76bd78ec9fae. Both Python and browser consumers use that pin. Adaptive echo suppression, 180 ms speech admission, retained onset, 2000 ms natural-pause behavior, WSS and the single central Live model are preserved. No second ASR, new provider, lexical noise filters or copied audio framework.

## Evidence boundary

Five of six new shared fixtures failed on the old code (one missing new API, plus existing lifecycle/finalization faults); all six pass after the patch. Full shared suite: 86/86 Node, 53/53 Python. GitHub contracts and native checks passed on the exact PR head. Read-only OpenCode review dvt_55b6200462cb4af282e8ddb5e7524e3a confirmed the playback and missing-listening faults. Its suggested playing.size-only cancellation test was not used: it would suppress valid first audio.

These are executable code reproductions, NOT proof that every physical-device incident had that exact cause. The historical 113-character Mira output proves provider text generation, not audibility on the phone.

The new product tests exercise the actual timing callback, pre-transcript rendering contract, independent finish/Stop controls, release trigger/target and shared dependency consistency. Clean local npm ci installed 0.3.23; the first web test run caught the changed legacy placeholder wording, restored without weakening the existing test. Local backend test collection selected system Python without project dependencies and failed; clean GitHub backend CI, which installs .[test], is the acceptance gate rather than that incomplete environment. Full product build/CI and post-deploy results are recorded separately once observed.

## Remaining product acceptance

True visible partial words during uninterrupted input remain NOT delivered. The new status and optional end button improve feedback/control but are not a substitute for V03. Do not mark the whole voice UX complete on the strength of these changes.

Physical acceptance needs: keyboard clicks/finger snaps while silent do not create a user bubble; beginning/end of normal Russian speech survive; Mira speaks a complete answer while the user is silent; deliberate interruption works; status returns to listening; another utterance works without restarting the mic; explicit finish yields a transcript/reply without closing the session. Device speaker/acoustic acceptance cannot be established by prepared-PCM canaries or a desktop browser fixture.

Release verification requires runtime health/version, clean CI, a public signed APK and update.json from the intended source, plus a short same-provider WSS round trip. Existing Android self-update E2E is automatically triggered by a successful release; report its actual status separately.
