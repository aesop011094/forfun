#!/usr/bin/env python3
"""Acceptance smoke of the running app on desktop and mobile.

Run: .venv/bin/python scripts/browser_smoke.py --base-url http://127.0.0.1:8000
Evidence is saved below data/qa. Default mode is read-only; --admin-predict
explicitly performs one prediction job using existing local authentication.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def ready(page) -> None:
    # Hash navigation does not reset the browser load lifecycle. Wait for React
    # to launch its request and for the actual route data to replace the loader.
    page.wait_for_timeout(180)
    page.locator('.loading-state').first.wait_for(state='hidden', timeout=45000)
    page.wait_for_load_state('networkidle')
    assert not page.locator('.error-state').count(), 'Route displayed an API error'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--output', default='data/qa')
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    parser.add_argument('--admin-predict', action='store_true', help='Explicitly run one real predict job from the desktop settings button')
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    base_url = args.base_url.rstrip('/')
    report: dict = {'base_url': base_url, 'viewports': [], 'errors': []}

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium, headless=True, args=['--no-sandbox'])
        for label, width, height in [('desktop', 1440, 1000), ('mobile', 390, 844)]:
            context = browser.new_context(viewport={'width': width, 'height': height}, device_scale_factor=1)
            page = context.new_page()
            errors: list[str] = []
            failed_responses: list[dict] = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('response', lambda response: failed_responses.append({'status': response.status, 'path': urlparse(response.url).path}) if response.status >= 500 else None)
            response = page.goto(base_url, wait_until='networkidle')
            ready(page)
            assert response and response.status == 200, f'{label}: initial HTTP status {response.status if response else None}'
            assert page.locator('body').inner_text().strip(), f'{label}: page is empty'
            displayed_totals = page.locator('.probability-labels').evaluate_all("groups => groups.map(group => [...group.querySelectorAll('b')].reduce((sum, node) => sum + parseFloat(node.textContent), 0))")
            assert all(abs(total - 100) < 1e-6 for total in displayed_totals), 'Displayed 1X2 percentages do not total 100.0%'
            page.screenshot(path=str(output / f'{label}-dashboard.png'), full_page=True)
            page.screenshot(path=str(output / f'{label}-dashboard-viewport.png'))
            drawer_checked = False
            if label == 'mobile':
                menu = page.get_by_role('button', name='開啟主要選單')
                if menu.count() and menu.is_visible():
                    menu.click()
                    assert page.locator('aside.sidebar').evaluate("el => el.classList.contains('open')"), 'Mobile drawer did not open'
                    page.get_by_role('button', name='關閉選單').last.click()
                    drawer_checked = True
            filter_checks = []
            league_button = page.get_by_role('button', name='英超', exact=True)
            if league_button.count():
                with page.expect_response(lambda response: '/api/matches?' in response.url and 'league=PL' in response.url):
                    league_button.click()
                filter_checks.append('league')
                with page.expect_response(lambda response: '/api/matches?' in response.url and 'q=Arsenal' in response.url):
                    page.get_by_label('搜尋球隊或比賽…', exact=True).fill('Arsenal')
                filter_checks.append('team_search')
                with page.expect_response(lambda response: '/api/matches?' in response.url and 'sort=confidence' in response.url):
                    page.get_by_label('比賽排序', exact=True).select_option('confidence')
                filter_checks.append('confidence_sort')
                page.get_by_role('button', name='清除搜尋', exact=True).click()
                with page.expect_response(lambda response: '/api/matches?' in response.url and 'date=' in response.url):
                    page.get_by_role('button', name='明日', exact=True).click()
                filter_checks.append('tomorrow_date')
                page.get_by_role('button', name='未來 7 日', exact=True).click()
                page.get_by_role('button', name='全部聯賽', exact=True).click()
                page.wait_for_timeout(300)
            pages = []
            hrefs = page.locator('a[href]').evaluate_all("els => [...new Set(els.map(el => el.getAttribute('href')))]")
            hrefs = [href for href in hrefs if href in {'#/', '#/teams', '#/history', '#/performance', '#/data', '#/settings'}]
            # Read-only navigation exercises the actual links furnished by the UI.
            for href in hrefs:
                if not href or href.startswith(('http:', 'https:', 'mailto:', 'tel:')) or href == '#':
                    continue
                page.goto(base_url + href if href.startswith('#') else base_url + '/' + href.lstrip('/'), wait_until='networkidle')
                ready(page)
                text = page.locator('body').inner_text().strip()
                overflow = page.evaluate('document.documentElement.scrollWidth > window.innerWidth + 2')
                pages.append({'path': href, 'text_length': len(text), 'horizontal_overflow': overflow})
                assert len(text) > 20, f'{label}: empty route {href}'
                assert not overflow, f'{label}: horizontal overflow on {href}'
                if href == '#/performance':
                    assert page.get_by_role('columnheader', name='Log Loss ↓', exact=True).count(), 'Real performance table not loaded'
                    page.screenshot(path=str(output / f'{label}-performance.png'), full_page=True)
                    page.screenshot(path=str(output / f'{label}-performance-viewport.png'))
                if href == '#/settings':
                    page.screenshot(path=str(output / f'{label}-settings.png'), full_page=True)
                if href == '#/data':
                    sources = page.locator('details.source-details > summary')
                    if sources.count():
                        sources.first.click()
                    page.screenshot(path=str(output / f'{label}-data-status.png'), full_page=True)
            page.goto(base_url + '#/history', wait_until='networkidle')
            ready(page)
            # A real match card, when present, must open an actual detailed analysis.
            match_link = page.locator('a[href*="/matches/"], a[href*="/match/"]').first
            detail_checked = False
            if not match_link.count():
                page.goto(base_url + '#/', wait_until='networkidle')
                all_records = page.get_by_role('button', name='所有已收錄', exact=True)
                if all_records.count():
                    all_records.click()
                    page.wait_for_timeout(700)
                    page.wait_for_load_state('networkidle')
            if match_link.count():
                match_link.click()
                ready(page)
                detail_checked = '/match' in (urlparse(page.url).path + urlparse(page.url).fragment)
                assert detail_checked, f'{label}: match detail did not navigate'
                page.screenshot(path=str(output / f'{label}-match-detail.png'), full_page=True)
                assert page.get_by_text('模型估計', exact=False).count(), 'Detail contains no honest model labeling'
                matrix_button = page.get_by_role('button', name='查看比分概率矩陣', exact=True)
                if matrix_button.count():
                    matrix_button.click()
                    assert page.locator('.matrix-table').is_visible(), 'Matrix toggle did not work'
                    page.get_by_role('button', name='收起比分概率矩陣', exact=True).click()
            overflow = page.evaluate('document.documentElement.scrollWidth > window.innerWidth + 2')
            assert not overflow, f'{label}: horizontal overflow in detail/dashboard'
            page.goto(base_url + '#/teams', wait_until='networkidle')
            ready(page)
            team_link = page.locator('a[href^="#/teams/"]').first
            team_detail_checked = False
            if team_link.count():
                team_link.click()
                ready(page)
                team_detail_checked = '/teams/' in urlparse(page.url).fragment
                assert team_detail_checked, 'Team detail did not open'
                assert page.get_by_role('heading', name='最近 5 場', exact=True).count(), 'Team form section missing'
                assert not page.get_by_text('undefined', exact=False).count(), 'Team detail contains undefined fields'
                page.screenshot(path=str(output / f'{label}-team-detail.png'), full_page=True)
            admin_job = None
            if args.admin_predict and label == 'desktop':
                page.goto(base_url + '#/settings', wait_until='networkidle')
                ready(page)
                configured = context.request.get(base_url + '/api/settings').json().get('admin_configured')
                if configured:
                    token = os.environ.get('ADMIN_TOKEN', '')
                    assert token, 'Admin token configured but not available to smoke runner'
                    page.get_by_label('管理密碼（ADMIN_TOKEN）', exact=True).fill(token)
                with page.expect_response(lambda response: response.request.method == 'POST' and '/api/admin/predict' in response.url) as submitted:
                    page.get_by_role('button', name='更新模型預測', exact=True).click()
                assert submitted.value.status == 202, 'Settings predict action was not accepted'
                job_id = submitted.value.json()['job_id']
                page.locator('.active-job').get_by_text('已完成', exact=True).wait_for(timeout=60000)
                result = context.request.get(base_url + '/api/jobs/' + job_id).json()
                assert result['status'] == 'completed', 'Actual prediction job did not complete'
                assert not result.get('result', {}).get('errors'), 'Prediction job has model errors'
                admin_job = {'job_id': job_id, 'status': result['status'], 'new_snapshots': result.get('result', {}).get('created')}
                page.screenshot(path=str(output / 'desktop-settings-completed.png'), full_page=True)
            # Explicitly prove the error state is usable when the fixture API fails.
            page.route('**/api/matches?*', lambda route: route.abort())
            page.goto(base_url + '#/', wait_until='networkidle')
            page.reload(wait_until='networkidle')
            page.wait_for_timeout(700)
            page.get_by_role('heading', name='未能讀取資料', exact=True).wait_for(timeout=5000)
            assert page.get_by_role('button', name='重新嘗試', exact=True).is_visible(), 'No retry button for API failure'
            page.screenshot(path=str(output / f'{label}-api-failure.png'), full_page=True)
            page.unroute('**/api/matches?*')
            page.get_by_role('button', name='重新嘗試', exact=True).click()
            page.wait_for_timeout(500)
            page.wait_for_load_state('networkidle')
            assert not page.get_by_role('heading', name='未能讀取資料', exact=True).count(), 'API retry did not recover'
            report['viewports'].append({'name': label, 'width': width, 'height': height, 'pages': pages, 'match_detail_checked': detail_checked, 'team_detail_checked': team_detail_checked, 'mobile_drawer_checked': drawer_checked, 'fixture_filters_checked': filter_checks, 'displayed_probability_groups': len(displayed_totals), 'api_failure_and_retry_checked': True, 'admin_prediction_job': admin_job, 'js_errors': errors, 'server_errors': failed_responses})
            report['errors'].extend(errors + [str(item) for item in failed_responses])
            context.close()
        browser.close()
    (output / 'browser-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    assert not report['errors'], 'JavaScript/server errors occurred; see evidence report'


if __name__ == '__main__':
    main()
