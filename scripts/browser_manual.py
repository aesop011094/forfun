#!/usr/bin/env python3
"""Exercise manual analysis against an isolated copy of the real packaged seed.

The tested pairings are hypothetical inputs, never published forecast evidence.
Run a scheduler-disabled server using the same isolated --database first.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import io
import json
from pathlib import Path
import re
import sqlite3

from playwright.sync_api import sync_playwright


def displayed_total(locator) -> None:
    values = [float(value) for value in re.findall(r'(\d+(?:\.\d+)?)%', locator.inner_text())]
    assert values and abs(sum(values) - 100) < 1e-6, values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8123')
    parser.add_argument('--database', type=Path, default=Path('data/qa/manual-run/football.db'))
    parser.add_argument('--output', type=Path, default=Path('data/qa'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip('/')
    with sqlite3.connect(f'file:{args.database.resolve()}?mode=ro', uri=True) as connection:
        rows = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload FROM matches WHERE league='PL' AND status='FINISHED' ORDER BY kickoff DESC LIMIT 90")]
    assert len(rows) == 90, 'The real packaged database is needed for this smoke test'
    counts = Counter(team for row in rows for team in (row['home'], row['away']))
    home, away = ('Arsenal', 'Manchester City') if min(counts['Arsenal'], counts['Manchester City']) >= 5 else [name for name, n in counts.most_common(2)]
    content = io.StringIO(newline='')
    writer = csv.writer(content)
    writer.writerow(['kickoff', 'home', 'away', 'home_goals', 'away_goals', 'available_at'])
    writer.writerows([row['kickoff'], row['home'], row['away'], row['home_goals'], row['away_goals'], row['result_available_at']] for row in rows)
    csv_bytes = ('\ufeff' + content.getvalue()).encode('utf-8')
    report: dict = {'scope': 'Hypothetical manual inputs; isolated real CC0 seed copy; not forecasting performance', 'viewports': []}

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
        for label, width, height in [('desktop', 1440, 1000), ('mobile', 390, 844)]:
            context = browser.new_context(viewport={'width': width, 'height': height}, timezone_id='America/Los_Angeles')
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/#/manual', wait_until='networkidle')
            page.get_by_role('heading', name='手動分析', exact=True).wait_for()
            assert page.locator('a[href="#/manual"]').count() == 1
            before = context.request.get(base + '/api/status').json()['counts']

            def mode(value):
                page.locator('.manual-mode').filter(has_text=value).click()

            def fill_match(h, a, kickoff='2027-06-01T20:00'):
                page.get_by_label('主隊名稱', exact=True).fill(h)
                page.get_by_label('客隊名稱', exact=True).fill(a)
                page.get_by_label('開賽日期及時間（香港）', exact=True).fill(kickoff)
                page.get_by_label('資料來源（選填）', exact=True).fill('QA 功能驗證；自訂對賽並非已核實真實賽程。')

            def submit(status=201):
                with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/api/manual/analyses'), timeout=60000) as pending:
                    page.get_by_role('button', name='分析並儲存', exact=True).click()
                response = pending.value
                assert response.status == status, response.text()
                if status == 201:
                    page.get_by_role('heading', name='手動分析結果', exact=True).wait_for()
                    page.get_by_role('button', name='分析並儲存', exact=True).wait_for(state='visible')
                    page.wait_for_load_state('networkidle')
                    return response.json()
                page.locator('.manual-error').filter(has_text='數據不足').wait_for()
                return None

            mode('自訂入球情境')
            fill_match('功能測試主隊', '功能測試客隊')
            page.get_by_label('主隊預期入球（假設）', exact=True).fill('1.80')
            page.get_by_label('客隊預期入球（假設）', exact=True).fill('0.90')
            notes = '未經核實的測試筆記 <img src=x onerror=alert(1)>；不能當成傷停消息。'
            page.get_by_label('補充資料／傷停筆記（選填）', exact=True).fill(notes)
            first = submit()
            assert first['input']['kickoff'].startswith('2027-06-01T20:00:00+08:00')
            assert abs(first['prediction']['expected_goals']['home'] - 1.8) < 1e-9
            assert page.locator('.manual-raw-notes').inner_text() == notes
            assert page.locator('.manual-raw-notes img').count() == 0
            displayed_total(page.locator('.manual-result .outcome-grid'))
            displayed_total(page.locator('.manual-result .btts-row'))
            displayed_total(page.locator('.manual-result .distribution-chart'))
            assert page.locator('.manual-result .score-list>div').count() == 5
            for row in page.locator('.manual-result .handicap-table tbody tr').all():
                displayed_total(row)
            page.get_by_role('button', name='查看完整比分概率矩陣', exact=True).click()
            displayed_total(page.locator('.manual-matrix tbody'))
            page.get_by_role('button', name='收起完整比分概率矩陣', exact=True).click()
            assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth + 2')
            page.locator('.manual-result-heading').scroll_into_view_if_needed()
            page.screenshot(path=str(args.output / f'manual-{label}-result.png'))

            # Changing inputs appends a new version; reopening restores the old input.
            page.get_by_label('主隊預期入球（假設）', exact=True).fill('2.40')
            second = submit()
            assert first['id'] != second['id']
            assert second['prediction']['probabilities']['home'] > first['prediction']['probabilities']['home']
            assert context.request.get(base + '/api/manual/analyses/' + first['id']).json() == first
            page.reload(wait_until='networkidle')
            saved = page.locator('.manual-saved').filter(has_text='功能測試主隊')
            assert saved.count() >= 2
            saved.nth(1).click()
            page.get_by_role('heading', name='手動分析結果', exact=True).wait_for()
            assert page.get_by_label('主隊預期入球（假設）', exact=True).input_value() == '1.8'

            if label == 'desktop':
                mode('輸入歷史賽果')
                with page.expect_download() as download:
                    page.get_by_role('button', name='下載 CSV 範本', exact=True).click()
                template = Path(download.value.path()).read_text(encoding='utf-8-sig')
                assert template.strip() == 'kickoff,home,away,home_goals,away_goals,available_at'
                file_input = page.get_by_label('匯入歷史賽果 CSV', exact=True)
                file_input.set_input_files({'name': 'invalid.csv', 'mimeType': 'text/csv', 'buffer': b'kickoff,home,away,home_goals,away_goals\n2026-02-30T20:00+08:00,A,B,1,0\n'})
                page.locator('.manual-error').filter(has_text='日期').wait_for()
                assert page.locator('.manual-history-row').count() == 1
                file_input.set_input_files({'name': 'real-cc0-history.csv', 'mimeType': 'text/csv', 'buffer': csv_bytes})
                page.locator('.manual-import-message').filter(has_text='90 場').wait_for()
                assert page.locator('.manual-history-row').count() == 90
                page.get_by_role('button', name='新增一場賽果', exact=True).click()
                assert page.locator('.manual-history-row').count() == 91
                page.get_by_role('button', name='刪除賽果 91', exact=True).click()
                assert page.locator('.manual-history-row').count() == 90
                fill_match(home, away)
                custom = submit()
                assert custom['provenance']['history_count'] == 90
                assert custom['provenance']['validated'] is False
                assert custom['prediction']['diagnostics']['training_matches'] == 90
                mode('使用已收錄賽果')
                fill_match('Arsenal', 'Manchester City')
                history = submit()
                assert history['provenance']['history_count'] >= 80
                assert history['prediction']['models']['elo']
                fill_match('沒有歷史的測試球隊', 'Manchester City')
                submit(status=422)
                # Explicitly exercise the labelled retrospective research path.
                mode('自訂入球情境')
                fill_match('功能測試主隊', '功能測試客隊', '2022-01-01T20:00')
                page.get_by_label('主隊預期入球（假設）', exact=True).fill('0')
                page.get_by_label('客隊預期入球（假設）', exact=True).fill('0')
                retrospective = submit()
                assert retrospective['provenance']['retrospective'] is True
                assert retrospective['prediction']['probabilities']['draw'] == 1
                assert page.locator('.manual-result').get_by_text('事後建立的假設分析', exact=False).count()
                page.route('**/api/manual/analyses', lambda route: route.abort() if route.request.method == 'POST' else route.continue_())
                page.get_by_role('button', name='分析並儲存', exact=True).click()
                page.locator('.manual-error').filter(has_text='未能連接').wait_for()
                page.unroute('**/api/manual/analyses')
            else:
                # The mode controls, CSV rows and saved list also fit a phone viewport.
                mode('輸入歷史賽果')
                page.screenshot(path=str(args.output / 'manual-mobile-history-form.png'), full_page=True)
                assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth + 2')
                page.get_by_role('button', name='開啟主要選單', exact=True).click()
                assert page.locator('aside.sidebar.open').count() == 1
                page.get_by_role('button', name='關閉選單').last.click()

            after = context.request.get(base + '/api/status').json()['counts']
            assert before == after, 'Manual workspace polluted official data/performance counts'
            assert not errors, errors
            report['viewports'].append({'name': label, 'width': width, 'scenario_and_snapshot_restore': True,
                                        'rounded_markets_and_matrix_total_100': True, 'no_page_overflow': True,
                                        'raw_notes_escape_html': True, 'official_counts_unchanged': True,
                                        'real_history_and_csv_models': label == 'desktop', 'js_errors': errors})
            context.close()
        browser.close()
    (args.output / 'manual-browser-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
