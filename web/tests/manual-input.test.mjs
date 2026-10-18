import assert from 'node:assert/strict';
import {test} from 'node:test';
import {hkLocal,hkISO,parseCSV} from '../src/manual-input.ts';

test('CSV preserves quoted team names, escaped quotes, UTF-8 BOM and CRLF',()=>{
  assert.deepEqual(parseCSV('\uFEFFhome,away,home_goals\r\n"Club, United","Club ""B""",2\r\n'),[
    ['home','away','home_goals'],['Club, United','Club "B"','2'],
  ]);
  assert.deepEqual(parseCSV('home\taway\n香港隊\t客隊\n'),[['home','away'],['香港隊','客隊']]);
});

test('CSV rejects broken quoting instead of silently renaming a team',()=>{
  for(const text of ['home,away\n"Unclosed,B','home,away\n"A"x,B','home,away\nClu"b,B'])
    assert.throws(()=>parseCSV(text),/CSV/);
});

test('date conversion preserves availability seconds and milliseconds across timezones',()=>{
  const input='2026-10-18T23:59:59.125Z';
  const editable=hkLocal(input);
  assert.equal(editable,'2026-10-19T07:59:59.125');
  assert.equal(new Date(hkISO(editable)).getTime(),new Date(input).getTime());
  assert.equal(hkLocal('2026-10-18T14:00:13-04:00'),'2026-10-19T02:00:13');
  assert.equal(hkLocal('2026-10-18'),'2026-10-18T00:00:00');
  assert.equal(hkISO('2026-10-18T20:00'),'2026-10-18T20:00:00+08:00');
});

test('impossible calendar dates and unsupported precision never change silently',()=>{
  assert.equal(hkLocal('2024-02-29T20:00+08:00'),'2024-02-29T20:00:00');
  for(const value of ['2026-02-30T20:00+08:00','2026-02-29','2026-13-01','2026-01-01T24:00','2026-01-01T00:00:00.123456Z'])
    assert.throws(()=>hkLocal(value),/日期|時間/);
});
