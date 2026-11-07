// Checks a data file with the same engine the page uses, before it replaces data.json.
// Exit codes: 0 publish, 2 invalid file, 3 choices can't be built (for example, an empty fund slot).
'use strict';
const fs = require('fs');
const path = require('path');
const E = require(path.join(__dirname, '..', 'site', 'engine.js'));

const file = process.argv[2];
let data;
try {
  data = JSON.parse(fs.readFileSync(file, 'utf8'));
} catch (e) {
  console.log('Not readable as JSON: ' + e.message);
  process.exit(2);
}

const errors = E.validateData(data);
if (errors.length) {
  console.log('Invalid data file:');
  errors.forEach((m) => console.log('- ' + m));
  process.exit(2);
}

const built = E.buildChoices(data);
const blocking = [];
const warnings = [];
built.forEach((b) => b.issues.forEach((m) => {
  const line = b.choice.name + ': ' + m;
  if (m.indexOf('No eligible fund') === 0 || m.indexOf('Only ') === 0) blocking.push(line); else warnings.push(line);
}));

const res = E.run(data, built, E.DEFAULT_INPUTS);
const money = (x) => (x < 0 ? '-$' : '+$') + Math.abs(x).toLocaleString('en-US');
console.log('Data as of ' + data.asOf + ', ' + data.holdings.length + ' holdings, months ' + data.months[0] + ' to ' + data.months[data.months.length - 1]);
console.log('Results for $10,000 over 1 year with default inputs:');
res.cards.forEach((c) => {
  console.log('  ' + c.name + ': typical ' + money(c.typical) + ', range ' + money(c.rangeLow) + ' to ' + money(c.rangeHigh) +
    ', worst ' + (c.worst ? money(c.worst.result) + ' (' + c.worst.name + ')' : 'n/a') + ', chance of a loss ' + Math.round(c.chanceOfLoss * 100) + '%');
  console.log('    holdings: ' + c.holdings.map((h) => h.ticker + ' $' + h.dollars).join(', '));
});
console.log('Checks:');
res.checks.forEach((c) => console.log('  ' + (c.pass ? 'Passed' : 'Needs attention') + ': ' + c.name));
warnings.forEach((m) => console.log('  Warning: ' + m));
blocking.forEach((m) => console.log('  Blocking: ' + m));
process.exit(blocking.length ? 3 : 0);
