import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const shell = readFileSync(new URL("../src/BoardShell.tsx", import.meta.url), "utf8");
const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const androidMain = readFileSync(
  new URL("../../android/app/src/main/java/com/kenigevents/projectshub/MainActivity.java", import.meta.url),
  "utf8",
);
const androidApi = readFileSync(
  new URL("../../android/app/src/main/java/com/kenigevents/projectshub/ApiClient.java", import.meta.url),
  "utf8",
);

test("owner share UI warns about live access and requires a user click", () => {
  assert.match(shell, /Живая ссылка · только просмотр/);
  assert.match(shell, /Срок — семь дней/);
  assert.match(shell, /дальнейшие изменения/);
  assert.match(shell, /const shareViaSystem = async \(\) =>/);
  assert.match(shell, /await navigator\.share\(/);
  assert.match(shell, /onClick=\{\(\) => void shareViaSystem\(\)\}/);
  assert.doesNotMatch(shell, /useEffect\([\s\S]{0,500}navigator\.share/);
  assert.match(shell, /доставка не выполнялась/);
  assert.match(shell, /не подтверждает доставку получателю/);
});

test("Mira share result only opens prepared-link UI", () => {
  assert.match(app, /"board_share"/);
  assert.match(app, /kind === "share" && action === "ready"/);
  assert.match(app, /setBoardShareRequest\(/);
  assert.match(app, /canManageShare=\{Boolean\(focusProject\.can_manage_share\)\}/);
  assert.match(app, /shareRequest=\{boardShareRequest\}/);
});


test("Android advertises only the narrow share chooser and never claims delivery", () => {
  assert.match(androidApi, /\.put\("share\.open_chooser"\)/);
  assert.match(androidMain, /"share\.open_chooser"\.equals\(command\.capability\)/);
  assert.match(androidMain, /Intent\.ACTION_SEND/);
  assert.match(androidMain, /Intent\.createChooser/);
  assert.match(androidMain, /\.put\("chooser_opened", true\)/);
  assert.match(androidMain, /\.put\("delivery_confirmed", false\)/);
  assert.doesNotMatch(androidMain, /delivery_confirmed", true/);
});
