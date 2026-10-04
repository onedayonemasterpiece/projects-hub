# Projects Hub Android MVP — acceptance 2026-10-04

Это короткий factual checkpoint после продуктовой доработки. Он не заменяет product docs и не объявляет физический Android PASS там, где физического устройства не было.

## Что реально принято

- production backend DevCoveer: exact release `771990de78f90fde555d880cd4f7838be1eb9df8`, service `active/running`, health PASS;
- public edge `https://projects-hub.kenigevents.ru` и public WSS: PASS;
- real Gemini Live WSS roundtrip: hello, binary PCM ACK, transcript, binary audio, turn complete, server close, HTTP fallback rejection — PASS;
- browser Live binding обновлён до `live-interaction v0.3.11`; long-speech regression выше старого ~48 kB watermark, clean `npm ci`, PWA production build — PASS;
- real-provider calendar server chain: Gemini Live → `calendar_create_event_on_device` → durable device command → simulated device `applied + readback_verified` → readiness-card → final Live transcript + audio — PASS;
- signed Android Release: `android-v7 / 0.1.7`;
- APK SHA-256: `9eedb0f825b486b3b1272a0f0b33c5f92686fc419a3895b000e8cf6946c19c62`;
- hosted Android 14 self-update: `android-v6 → android-v7` — PASS;
- self-update evidence: manifest SHA-256 verified, in-app update surfaced, APK downloaded and verified, Package Installer handoff, same-signature in-place update, UID preserved.

## Что ещё не является PASS

- physical Android microphone → public WSS → Live;
- physical Android Calendar API insert + provider readback;
- финальный пользовательский tap в системном Package Installer на physical device;
- физический first-party invite login после удаления WebView/Yandex/Supabase auth flow ещё должен быть проверен владельцем на установленном Android;
- GitHub App runtime credentials пока не configured; это не блокирует calendar MVP, но блокирует реальные GitHub product actions.

## Практический статус

Текущий Android shell уже установлен и загружает актуальную PWA с backend, поэтому исправление first-party login не требует переустановки APK. Supabase Auth и обязательный Яндекс OAuth удалены из user-login path; текущий owner bootstrap — одноразовый first-party invite. Следующий необходимый acceptance — один физический прогон: invite login → microphone → Live → calendar → readback.


## Physical microphone failure found on 4 October 2026

The first owner run authenticated successfully, created a Live session and connected public WSS, but Android WebView did not deliver microphone audio. The browser client then left that server session alive, so repeated start attempts on the same conversation returned HTTP 429 until the old lease released.

The correction preflights microphone capture before creating the server Live session (`captureDuringStart=true`) and explicitly stops local/server Live state on `microphone_error` or startup failure. A microphone denial therefore must not strand a session or turn subsequent taps into repeated 429s. Physical Android permission acceptance remains pending; the UI now directs the owner to the exact Android microphone permission when access is denied.
