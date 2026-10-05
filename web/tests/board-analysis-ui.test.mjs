import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const boardApi = readFileSync(new URL("../src/boardApi.ts", import.meta.url), "utf8");
const shell = readFileSync(new URL("../src/BoardShell.tsx", import.meta.url), "utf8");
const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");

test("board analysis UI uses isolated run API and renders markdown as text", () => {
  assert.match(boardApi, /\/api\/analysis\/runs/);
  assert.match(boardApi, /analysis_publish_ui_/);
  assert.match(shell, /Kimi K3/);
  assert.match(shell, /DeepSeek/);
  assert.match(shell, /board-analysis-report/);
  assert.match(shell, /<pre className="board-analysis-report">\{analysisRun\.result_markdown\}<\/pre>/);
  assert.doesNotMatch(shell, /dangerouslySetInnerHTML/);
  assert.match(shell, /canAnalyze && selected\.type !== "document_card"/);
  assert.match(shell, /Модель получает только замороженную версию выбранного объекта/);
});

test("Mira board_analysis opens the same board report panel", () => {
  assert.match(app, /"board_analysis"/);
  assert.match(app, /kind === "analysis" && action === "show"/);
  assert.match(app, /setAnalysisRunId\(runId\)/);
  assert.match(app, /canAnalyze=\{Boolean\(focusProject\.can_analyze\)\}/);
  assert.match(app, /analysisRunId=\{analysisRunId\}/);
});
