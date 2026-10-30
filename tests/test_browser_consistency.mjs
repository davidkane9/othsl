import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const template = fs.readFileSync(new URL('../templates/team.html', import.meta.url), 'utf8');
const workerSource = template.split('const WORKER_SRC = `')[1].split('\n`;')[0];
const messages = [];
const workerContext = vm.createContext({ self: { postMessage: message => messages.push(message) } });
vm.runInContext(workerSource, workerContext);
const request = {
  teams: ['A', 'B'], currentStats: { A: { pts: 0, gd: 0, gf: 0 }, B: { pts: 0, gd: 0, gf: 0 } },
  currentElos: { A: 1500, B: 1500 }, remainingGames: [{ id: 'game', home: 'A', away: 'B' }],
  overrides: { game: 'away' }, selectedTeam: 'A', totalRuns: 10000,
  promotionCut: 1, relegationCut: 0, currentRegression: 0,
};
workerContext.self.onmessage({ data: request });
const stats = messages.find(message => message.type === 'team_stats').teamStats;
assert.equal(stats.A.promo, 0);
assert.equal(stats.B.promo, 100);
assert.equal(messages.find(message => message.type === 'progress' && message.done).completed, 10000);

const updateUI = template.slice(template.indexOf('function updateUI('), template.indexOf('function startSim('));
const runner = template.slice(template.indexOf('function startSim('), template.indexOf('\nstartSim();'));
const context = vm.createContext({ assert });
vm.runInContext(`
  var summary = { hidden: false };
  var document = { getElementById: () => summary };
  var SIM_DATA = { current_stats: {}, remaining_games: [] };
  var BASELINE_OUTLOOK = {
    total_runs: 10000, counts: { A: [0, 6100, 3900] },
    display_stats: { A: { promo: 61, stay: 39, relg: 0, modal: 1 } }
  };
  var TOTAL_RUNS = 10000, selected_team = 'A', teams = ['A','B'];
  var total_teams = 2, promotion_cut = 1, relegation_cut = 0, CURRENT_REGRESSION = 0;
  var current_stats = SIM_DATA.current_stats, remaining_games = SIM_DATA.remaining_games, current_elos = {};
  var overrides = {}, teamStats = {}, worker = null, workerStarts = 0, renders = 0;
  var elStatus = {}, elCounter = {}, elPctPromo = {}, elPctStay = {}, elPctRel = {};
  var elChipPromo = {}, elChipStay = {}, elChipRel = {}, fillEls = [{},{},{}], pctEls = [{},{},{}];
  for (var element of fillEls) element.style = {};
  var WORKER_SRC = '', Blob = class {}, URL = { createObjectURL: () => 'test' };
  var Worker = class { constructor() { workerStarts++; } postMessage() {} terminate() {} };
  function renderGrid() { renders++; }
  ${updateUI}
  ${runner}
  startSim();
  assert.equal(workerStarts, 0, 'Baseline must reuse server results, without a new random simulation');
  assert.equal(elPctPromo.textContent, '61%');
  assert.equal(elPctStay.textContent, '39%');
  assert.equal(teamStats.A.promo, 61);
  assert.equal(summary.hidden, false);
  overrides.game = 'away';
  startSim();
  assert.equal(workerStarts, 1);
  assert.equal(summary.hidden, true, 'Baseline commentary must not masquerade as scenario results');
  worker.onmessage({ data: { type: 'progress', completed: 10000, done: true, counts: [0,0,10000] } });
  assert.equal(elPctPromo.textContent, '0%');
  delete overrides.game;
  startSim();
  assert.equal(workerStarts, 1, 'Reset must not start a fresh random baseline');
  assert.equal(elPctPromo.textContent, '61%');
  assert.equal(summary.hidden, false);
  current_stats = {};
  startSim();
  assert.equal(workerStarts, 2);
  assert.equal(summary.hidden, true, 'Historical-week views must not show the current baseline summary');
`, context);
console.log('Browser simulation checks passed: worker outcomes, shared baseline, what-if, reset, and historical views.');
