#!/usr/bin/env python3
"""Read-only final checks of real UI market display and history pagination."""
import json
from pathlib import Path
import re

from playwright.sync_api import sync_playwright
from browser_smoke import ready

BASE = 'http://127.0.0.1:8000'
OUT = Path('data/qa')
OUT.mkdir(exist_ok=True)


def total_percent(locator):
    values = [float(value) for value in re.findall(r'(\d+(?:\.\d+)?)%', locator.inner_text())]
    assert values, 'No rendered percentages to validate'
    assert abs(sum(values) - 100) < 1e-7, values
    return len(values)


def main():
    report = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        for label,width,height in [('desktop',1440,1000),('mobile',390,844)]:
            context = browser.new_context(viewport={'width':width,'height':height})
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(BASE,wait_until='networkidle')
            ready(page)
            asset = page.locator('script[type="module"]').get_attribute('src')
            cards = page.locator('.probability-labels')
            assert cards.count()
            for i in range(cards.count()): total_percent(cards.nth(i))
            checked_cards = cards.count()
            page.screenshot(path=str(OUT / f'{label}-dashboard-viewport.png'))
            page.get_by_role('button',name='所有已收錄',exact=True).click()
            ready(page)
            first = page.locator('.match-teams').first.get_attribute('href')
            page.get_by_role('button',name='下一頁',exact=True).click()
            ready(page)
            assert page.locator('.match-teams').first.get_attribute('href') != first
            page.get_by_role('button',name='上一頁',exact=True).click()
            ready(page)
            assert page.locator('.match-teams').first.get_attribute('href') == first

            page.goto(BASE+'#/history',wait_until='networkidle')
            ready(page)
            compact = page.locator('.compact-probs')
            assert compact.count() == 100
            for i in range(compact.count()): total_percent(compact.nth(i))
            first_history = page.locator('.history-table a[href^="#/matches/"]').first.get_attribute('href')
            page.get_by_role('button',name='下一頁',exact=True).click()
            ready(page)
            assert page.locator('.history-table a[href^="#/matches/"]').first.get_attribute('href') != first_history
            page.get_by_role('button',name='上一頁',exact=True).click()
            ready(page)
            assert page.locator('.history-table a[href^="#/matches/"]').first.get_attribute('href') == first_history
            page.get_by_label('顯示記錄數量',exact=True).select_option('500')
            ready(page)
            assert page.locator('.history-table tbody tr').count() == 500
            page.locator('.history-table a[href^="#/matches/"]').first.click()
            ready(page)
            assert total_percent(page.locator('.outcome-grid')) == 3
            over_under = page.locator('section.panel').filter(has=page.get_by_role('heading',name='大小球分析',exact=True))
            for i in range(3): assert total_percent(over_under.locator('tbody tr').nth(i)) == 2
            assert total_percent(page.locator('.btts-row')) == 2
            handicap = page.locator('.handicap-table tbody tr')
            assert handicap.count() == 17
            for i in range(handicap.count()): assert total_percent(handicap.nth(i)) == 5
            distribution_size = total_percent(page.locator('.distribution-chart'))
            page.screenshot(path=str(OUT / f'{label}-match-detail.png'),full_page=True)
            page.get_by_role('button',name='查看比分概率矩陣',exact=True).click()
            matrix_size = total_percent(page.locator('.matrix-table tbody'))
            page.get_by_role('button',name='收起比分概率矩陣',exact=True).click()

            page.goto(BASE+'#/performance',wait_until='networkidle')
            ready(page)
            timeline = page.locator('.validation-timeline').inner_text()
            assert 'T00:00' not in timeline
            assert re.search(r'\d{2}/\d{2}/\d{4}',timeline)
            assert page.get_by_text('初始訓練、校準、驗證及最後測試依日期先後分開。',exact=False).count()
            market = page.locator('section.panel').filter(has=page.get_by_role('heading',name='入球市場的獨立測試',exact=True))
            rows = market.locator('tbody tr')
            assert rows.count() == 3
            reports = context.request.get(BASE+'/api/performance').json()['reports']
            selected = next(report for report in reports if report['league']=='PL')
            actual = next(model for model in selected['models'] if model['name']==selected['selected_model'])['market_metrics']
            for i,key in enumerate(('over_2_5','btts','correct_score')):
                cells = rows.nth(i).locator('td')
                assert int(cells.nth(1).inner_text().replace(',','')) == actual[key]['n']
                assert abs(float(cells.nth(2).inner_text()) - actual[key]['log_loss']) < .000051
                if key != 'correct_score': assert abs(float(cells.nth(3).inner_text()) - actual[key]['brier']) < .000051
            page.screenshot(path=str(OUT / f'{label}-performance.png'),full_page=True)
            page.screenshot(path=str(OUT / f'{label}-performance-viewport.png'))
            assert not page.evaluate('document.documentElement.scrollWidth > window.innerWidth + 2')
            assert not errors, errors
            report[label] = {'javascript_asset':asset,'card_probability_groups':checked_cards,'history_probability_groups':100,
                             'detail_1x2_over_under_btts_asian_total_goals_matrix_all_100_percent':True,
                             'asian_settlement_rows':handicap.count() if '/match' in page.url else 17,
                             'goal_distribution_bins':distribution_size,'matrix_cells':matrix_size,
                             'dashboard_and_history_next_previous_pass':True,'history_500_rows_accessible':True,
                             'market_test_rows_verified_against_real_report':3,'human_dates_and_chinese_methodology':True,'js_errors':errors}
            context.close()
        browser.close()
    (OUT/'browser-focused.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
