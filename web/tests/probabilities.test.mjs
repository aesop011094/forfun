import assert from 'node:assert/strict';
import test from 'node:test';
import { roundedProbabilities } from '../src/types.ts';
const total = values => roundedProbabilities(values).reduce((sum, label) => sum + Math.round(parseFloat(label) * 10), 0);
test('1X2 distributes BOTH residual tenths instead of stopping at 99.9%', () => {
  assert.deepEqual(roundedProbabilities([.3339, .3339, .3322]), ['33.4%', '33.4%', '33.2%']);
  assert.equal(total([.3339, .3339, .3322]), 1000);
});
test('complementary markets, Asian settlements and score matrices sum exactly 100.0%', () => {
  for (const values of [[.0525,.9475], [.12399,.00199,.33399,.11199,.42804], Array.from({length:625},(_,i)=>Math.exp(-i/16))]) {
    assert.equal(total(values), 1000);
  }
});
test('all three-class display combinations preserve total and rounding precision', () => {
  for(let home=0;home<=100;home++) for(let draw=0;draw<=100-home;draw++) {
    const values = [home/100,draw/100,(100-home-draw)/100];
    assert.equal(total(values),1000);
    const displayed=roundedProbabilities(values).map(value=>parseFloat(value)/100);
    displayed.forEach((value,i)=>assert.ok(Math.abs(value-values[i])<.00100000001));
  }
});
