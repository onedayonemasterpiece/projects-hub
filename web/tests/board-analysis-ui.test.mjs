import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const boardApi = readFileSync(new URL("../src/boardApi.ts", import.meta.url), "utf8");
const shell = readFileSync(new URL("../src/BoardShell.tsx", import.meta.url), "utf8");
const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");

test("board analysis report UI is read-only and renders markdown as text", () => {
  assert.match(boardApi, /\/api\/analysis\/runs/);
  assert.match(shell, /board-analysis-report/);
  assert.match(shell, /<pre className="board-analysis-report">\{analysisRun\.result_markdown\}<\/pre>/);
  assert.doesNotMatch(shell, /dangerouslySetInnerHTML/);

  assert.doesNotMatch(shell, /startAnalysis\(/);
  assert.doesNotMatch(shell, /publishAnalysisRun\(/);
  assert.doesNotMatch(shell, /cancelAnalysisRun\(/);
  assert.doesNotMatch(shell, /Запустить анализ/);
  assert.doesNotMatch(shell, /Добавить на доску/);
  assert.doesNotMatch(shell, /<textarea/);
  assert.doesNotMatch(shell, /Консилиум · Kimi \+ DeepSeek · NVIDIA/);

  assert.match(shell, /Два NVIDIA-слота заняты\. Консилиум ждёт свободный слот/);
  assert.match(shell, /Открыть отчёт/);
});

test("Mira board_analysis opens the same read-only board report panel", () => {
  assert.match(app, /"board_analysis"/);
  assert.match(app, /kind === "analysis" && action === "show"/);
  assert.match(app, /setAnalysisRunId\(runId\)/);
  assert.match(app, /canAnalyze=\{Boolean\(focusProject\.can_analyze\)\}/);
  assert.match(app, /analysisRunId=\{analysisRunId\}/);
});

test("analysis GitHub materialization is explicit, idempotency-aware and public-safe", () => {
  assert.match(boardApi, /getAnalysisMaterialization/);
  assert.match(boardApi, /materializeAnalysisRun/);
  assert.match(boardApi, /\/materialization\?/);
  assert.match(boardApi, /\/materialize/);
  assert.match(shell, /GitHub-копия отчёта/);
  assert.match(shell, /Внутренний Markdown уже сохранён/);
  assert.match(shell, /Синхронизировать в GitHub/);
  assert.match(shell, /Опубликовать отчёт в публичный GitHub/);
  assert.match(shell, /полный текст отчёта в публичном GitHub/);
  assert.match(shell, /без нового commit/);
  assert.doesNotMatch(shell, /useEffect\([\s\S]{0,1200}materializeAnalysisRun/);
  assert.match(shell, /onClick=\{\(\) =>[\s\S]{0,220}materializeCurrentAnalysis/);
});
