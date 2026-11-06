// Run: node test_states.mjs   (checks the request-status state machine in src/match.js)
import assert from 'node:assert/strict';
import { canMove, fromStates, REQUEST_STATUSES, FINAL_STATUSES } from './src/match.js';

// Bugs this table replaced (2026-11-06)
assert.ok(canMove('Fee estimate', 'Denied'), 'a fee estimate can still be denied');
assert.ok(!canMove('Appealed', 'Resubmit required'), 'a bounce-style reply must not overwrite an appeal');
assert.ok(!canMove('Records released', 'Resubmit required'), 'nor a released request');
assert.ok(!canMove('Denied', 'Acknowledged'), 'a denied request does not go back to working');
assert.ok(!canMove('Records released', 'Closed'), 'final statuses are frozen');
assert.ok(canMove('Appealed', 'Closed') && canMove('Appealed', 'Records released'), 'an appeal can end');
assert.ok(canMove('Awaiting reply', 'Acknowledged') && canMove('Resubmit required', 'In progress'));
assert.ok(!canMove('Acknowledged', 'Appealed'), 'Appealed is set by a human filing, never automatically');
assert.ok(!canMove('Acknowledged', 'Acknowledged') && !canMove('Bogus', 'Denied') && !canMove('Denied', 'Bogus') && !canMove(null, 'Denied'));
assert.ok(!fromStates('Denied', true).includes('Appealed') && fromStates('Denied').includes('Appealed'));
// No status is stuck: every non-final one can reach a final one.
for (const s of REQUEST_STATUSES) if (!FINAL_STATUSES.includes(s)) assert.ok(canMove(s, 'Closed'), s + ' can close');
console.log('foia states ok');
