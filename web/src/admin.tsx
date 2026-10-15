import { useEffect, useRef, useState } from 'react';
import { Activity, CalendarDays, Check, Clock3, Database, Download, ExternalLink, KeyRound, LoaderCircle, Play, RefreshCw, Target } from 'lucide-react';
import { request, useData } from './api';
import type { Bag } from './types';
import { count, dateTime, labelStatus, LEAGUES, leagueShort } from './types';
import { Badge, DataCounts, DetailRow, Empty, ErrorState, Loading, Notice, PageHeading, Panel } from './shared';

interface PageProps { revision: number; refresh: () => void }
const kindLabel = (kind: string) => ({sync:'同步足球數據',train:'訓練模型及回測',predict:'更新賽前預測'} as Bag)[kind] || kind;
const sourceLabel = (value: string) => value === 'openfootball' ? 'Openfootball · CC0 開放數據' : value === 'football-data.org' ? 'football-data.org · 帳戶 API' : value;
const sourceStatus = (value: string) => ({ok:'讀取成功',partial:'部分資料有缺口',error:'同步失敗'} as Bag)[value] || labelStatus(value);

function jobResultSummary(job: Bag): string {
  if (job.error) return job.error;
  const result = job.result;
  if (result?.errors?.length) return `有 ${result.errors.length} 項資料或預測問題，請查看數據狀態；已有資料保留。`;
  if (result?.ingested != null) return `已同步 ${count(result.ingested)} 場比賽`;
  if (result?.created != null) return `已儲存 ${count(result.created)} 個預測`;
  if (Array.isArray(result?.reports)) {
    const complete = result.reports.filter((report: Bag) => report.status === 'completed').length;
    return `${complete} / ${result.reports.length} 個聯賽完成有效驗證${complete < result.reports.length ? '；其餘數據不足' : ''}`;
  }
  return job.status === 'running' ? '正在處理真實資料' : job.status === 'pending' ? '等候前一項工作完成' : '已保存結果';
}

function JobsTable({jobs}: {jobs: Bag[]}) {
  if (!jobs.length) return <Empty title="尚未有自動化工作記錄">同步、訓練及預測的執行狀態會在這裏保留，包括失敗訊息及重試時間。</Empty>;
  return <div className="table-wrap"><table><thead><tr><th>工作</th><th>狀態</th><th>開始時間（HKT）</th><th>完成時間</th><th>結果／錯誤</th></tr></thead><tbody>{jobs.map(job => <tr key={job.id}>
    <td><b>{kindLabel(job.kind)}</b>{job.params?.league && <small>{job.params.league === 'all' ? '全部聯賽' : leagueShort(job.params.league)}</small>}</td>
    <td><Badge tone={job.status === 'completed' ? 'mint' : job.status === 'failed' ? 'amber' : 'blue'}>{labelStatus(job.status)}</Badge></td>
    <td>{dateTime(job.started_at || job.created_at)}</td><td>{job.completed_at ? dateTime(job.completed_at) : '尚未完成'}</td>
    <td>{jobResultSummary(job)}{job.next_retry_at && <small>下次重試：{dateTime(job.next_retry_at)}</small>}</td>
  </tr>)}</tbody></table></div>;
}

function downloadStatus(status: Bag) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(status, null, 2)], {type:'application/json'}));
  const anchor = document.createElement('a'); anchor.href = url;
  anchor.download = 'football-data-status.json'; anchor.click(); URL.revokeObjectURL(url);
}

export function DataStatusPage({revision, refresh, status, statusError}: PageProps & {status: Bag | null; statusError: string}) {
  const own = useData<Bag>('/api/status', revision);
  const data = own.data || status; const error = own.error || statusError;
  const sources: Bag[] = data?.sources || [];
  const warnings: string[] = [...new Set<string>([...(data?.issues || []), ...sources.flatMap(source => source.warnings || [])])];
  const runs: Bag = data?.scheduler?.next_runs || {};
  return <>
    <PageHeading eyebrow="DATA TRANSPARENCY" title="數據更新狀態" description="查看資料的真正來源、更新時間，以及每次自動化工作的結果。" action={<button className="button secondary" onClick={() => {own.reload(); refresh();}}><RefreshCw size={15}/>重新整理</button>}/>
    {error && <ErrorState message={error} reload={own.reload}/>}
    {!data && !error ? <Loading/> : data && <>
      <DataCounts counts={data.counts || {}}/>
      <Notice tone="warning">社群賽程未經官方逐場核實，可能變更；有日期不代表有已確認開賽時間。模型缺少真實 xG、傷停及正選資料，所有預測維持研究用途。</Notice>
      {warnings.map((warning, index) => <Notice tone="warning" key={`${index}-${warning}`}>{warning}</Notice>)}
      <Panel title="來源與新鮮程度" subtitle="下載成功與賽果是否最新，是兩項不同的檢查" action={<button className="button secondary" onClick={() => downloadStatus(data)}><Download size={14}/>匯出狀態</button>}>
        {!sources.length ? <Empty title="尚未接入資料">請到管理設定啟動 CC0 公開數據同步，毋須 API 金鑰或付費。</Empty> : sources.map(source => {
          const datasets: Bag[] = source.sources || [];
          const latest: Bag = source.latest_result_dates || {};
          return <div className="source-card" key={source.name}>
            <div className="panel-head"><div><h3>{sourceLabel(source.name)}</h3><p>{source.name === 'openfootball' ? '可供模型研究的 CC0 社群數據；上游由人手更新' : '須由帳戶持有人確認目前方案的用途授權'}</p></div><Badge tone={source.status === 'ok' ? 'mint' : 'amber'}>{sourceStatus(source.status)}</Badge></div>
            <DetailRow label="最後同步" value={dateTime(source.last_sync)}/><DetailRow label="本次同步場數" value={count(source.ingested)}/>
            {Object.entries(latest).map(([league, moment]) => <DetailRow key={league} label={`${leagueShort(league)} 最新已收錄賽果`} value={typeof moment === 'string' ? `${moment.slice(0,10)}${Date.now() - new Date(moment).getTime() > 7 * 86400000 ? ' · 資料已滯後' : ''}` : '數據不足'}/>)}
            {(source.errors || []).map((message: string, index: number) => <Notice tone="warning" key={index}>{message}</Notice>)}
            {source.next_retry_at && <DetailRow label="下次重試" value={dateTime(source.next_retry_at)}/>}
            {!!datasets.length && <details className="source-details"><summary>查看 {count(datasets.length)} 個來源檔案及完整度</summary><div className="table-wrap"><table><thead><tr><th>聯賽／球季</th><th>比賽／完場</th><th>未收錄過去賽果</th><th>已排除資料</th><th>來源版本</th><th>原始來源</th></tr></thead><tbody>{datasets.map((dataset, index) => <tr key={`${dataset.league}-${dataset.season}-${index}`}>
              <td>{leagueShort(dataset.league)}<small>{dataset.season || 'API 時間窗口'}</small></td><td>{count(dataset.accepted)} / {count(dataset.finished)}</td><td>{count(dataset.missing_past_results)}</td><td>{count(dataset.rejected)}</td><td><code title={dataset.commit || dataset.sha256}>{(dataset.commit || dataset.sha256 || '—').slice(0,12)}</code></td><td>{typeof dataset.url === 'string' && dataset.url.startsWith('https://') ? <a className="text-link" href={dataset.url} target="_blank" rel="noreferrer">原始資料<ExternalLink size={12}/></a> : '未提供'}</td>
            </tr>)}</tbody></table></div><p className="panel-note">固定來源版本及檔案驗證碼用於重現研究和檢查檔案完整性，不代表已獲官方核實。完整驗證碼可在匯出的狀態查看。</p></details>}
          </div>;
        })}
      </Panel>
      <div className="two-column"><Panel title="自動更新安排" subtitle="排程只在伺服器運作期間執行">
        <DetailRow label="排程狀態" value={<Badge tone={data.scheduler?.enabled ? 'mint' : 'amber'}>{data.scheduler?.enabled ? '已啟用' : '未啟用'}</Badge>}/>
        {(['sync','predict','train'] as const).map(kind => <DetailRow key={kind} label={kindLabel(kind)} value={<span>{data.scheduler?.intervals?.[kind] || '未提供'}<small>下次：{dateTime(runs[kind])}</small></span>}/>)}
        <p className="panel-note">只有數據足夠及有新賽果時才重新訓練。API 失敗會保留已有資料並記錄錯誤；沒有可靠正選來源時，不宣稱已套用陣容更新。</p>
      </Panel><Panel title="可取得的資料"><DetailRow label="歷史賽果" value={data.capabilities?.real_results ? '已接入真實來源' : '尚未同步'}/><DetailRow label="官方 API 賽程" value={data.capabilities?.official_fixtures ? '已有帳戶 API 記錄' : '尚未接入；目前使用社群賽程'}/><DetailRow label="真實 xG／xGA" value="數據不足"/><DetailRow label="已確認傷停／正選" value="數據不足"/><DetailRow label="預測類型" value="只限比賽開始前"/><p className="panel-note">歷史結果的當時發布版本未能證實，回測採用日期後 36 小時的保守假設；研究結果與真正賽前快照分開記錄。</p></Panel></div>
      <Panel title="最近工作記錄" subtitle="結果及錯誤由後端實際工作產生"><JobsTable jobs={data.jobs || []}/></Panel>
    </>}
  </>;
}

function useAdminJob(adminToken: string, refresh: () => void) {
  const [job,setJob] = useState<Bag | null>(null); const [submitting,setSubmitting] = useState(false); const [error,setError] = useState('');
  const finished = useRef(refresh); finished.current = refresh;
  useEffect(() => {
    if (!job?.id || !['pending','running'].includes(job.status)) return;
    let active = true; let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await request<Bag>(`/api/jobs/${encodeURIComponent(job.id)}`);
        if (!active) return;
        setJob(next); setError('');
        if (!['pending','running'].includes(next.status)) {finished.current(); return;}
      } catch (reason) {
        if (!active) return;
        setError(reason instanceof Error ? reason.message : '未能讀取工作進度，正在重新嘗試。');
      }
      if (active) timer = window.setTimeout(poll, 2500);
    };
    void poll();
    return () => {active = false; window.clearTimeout(timer);};
  }, [job?.id, job?.status]);
  const run = async (kind: string, params: Bag = {}) => {
    setSubmitting(true); setError('');
    try {
      const result = await request<Bag>(`/api/admin/${kind}`, {method:'POST', body:JSON.stringify(params), headers:adminToken ? {'X-Admin-Token':adminToken} : {}});
      setJob({id:result.job_id, kind, status:result.status, created_at:new Date().toISOString()});
    } catch (reason) {setError(reason instanceof Error ? reason.message : '未能啟動工作，請稍後再試。');}
    finally {setSubmitting(false);}
  };
  return {job,run,error,busy:submitting || !!job && ['pending','running'].includes(job.status)};
}

export function SettingsPage({revision,refresh,adminToken,setAdminToken}: PageProps & {adminToken: string; setAdminToken: (value: string) => void}) {
  const settings = useData<Bag>('/api/settings', revision); const status = useData<Bag>('/api/status', revision);
  const [provider,setProvider] = useState('openfootball'); const [league,setLeague] = useState('all');
  const work = useAdminJob(adminToken, refresh); const data = settings.data;
  const blocked = work.busy || !data || !!data.admin_configured && !adminToken;
  const apiReady = !!data?.api_key_configured && !!data?.terms_accepted;
  return <>
    <PageHeading eyebrow="WORKSPACE SETTINGS" title="管理設定" description="同步真實資料、訓練並驗證模型，再儲存比賽前預測。" action={<button className="button secondary" onClick={() => {settings.reload(); status.reload(); refresh();}}><RefreshCw size={15}/>重新整理設定</button>}/>
    {settings.error ? <ErrorState message={settings.error} reload={settings.reload}/> : settings.loading && !data ? <Loading/> : data && <>
      <Notice>預設使用免費 CC0 開放資料，毋須申請帳戶。本程式沒有啟用任何付費服務；API 的價格和授權須先由你確認。</Notice>
      <div className="two-column"><Panel title="管理存取" subtitle="管理密碼只保留於目前頁面的記憶體">
        <DetailRow label="伺服器管理保護" value={<Badge tone={data.admin_configured ? 'mint' : 'amber'}>{data.admin_configured ? '已設定管理密碼' : '只接受本機管理要求'}</Badge>}/>
        <label className="settings-label" htmlFor="admin-token">管理密碼（ADMIN_TOKEN）</label><div className="search-field"><KeyRound size={16}/><input id="admin-token" type="password" autoComplete="off" value={adminToken} onChange={event => setAdminToken(event.target.value)} placeholder="已設定密碼時才需要輸入"/></div>
        <p className="panel-note">這裏不是足球 API 金鑰輸入欄。管理密碼不會存入瀏覽器儲存空間；關閉或重新載入頁面後需再輸入。公開部署前，請在平台 Secrets 設定 ADMIN_TOKEN。</p>
      </Panel><Panel title="目前設定"><DetailRow label="預設數據來源" value={sourceLabel(data.provider)}/><DetailRow label="足球 API 金鑰" value={data.api_key_configured ? '伺服器已設定（不顯示內容）' : '未設定；免費社群來源可繼續使用'}/><DetailRow label="API 用途授權確認" value={data.terms_accepted ? '帳戶持有人已確認' : '尚未確認'}/><DetailRow label="自動更新" value={data.scheduler?.enabled ? '已啟用' : '未啟用'}/><p className="panel-note">管理操作保留執行記錄；已有預測版本不會被修改或刪除。</p></Panel></div>
      <Panel title="數據及模型工作" subtitle="按順序操作；如果已有真實資料及模型，可以直接更新預測">
        <div className="settings-action-grid">
          <div className="settings-action"><div className="metric-icon blue"><Database size={20}/></div><h3>1. 同步足球數據</h3><p>讀取賽程及比分，驗證來源後保存至資料庫。完成後自動更新未來賽事預測。</p><label className="select-wrap"><select aria-label="選擇同步數據來源" value={provider} onChange={event => setProvider(event.target.value)}><option value="openfootball">Openfootball · 免費 CC0</option><option value="football-data.org">football-data.org · 已授權帳戶</option></select></label><button className="button primary" disabled={blocked || provider === 'football-data.org' && !apiReady} onClick={() => void work.run('sync',{provider})}><RefreshCw size={15}/>同步真實資料</button>{provider === 'football-data.org' && !apiReady && <p className="muted">須先在平台 Secrets 設定 API 金鑰及確認用途條款。</p>}</div>
          <div className="settings-action"><div className="metric-icon purple"><Activity size={20}/></div><h3>2. 訓練及歷史回測</h3><p>以真實歷史賽果比較模型，按時間驗證並選擇配置；完成後更新預測。</p><label className="select-wrap"><select aria-label="選擇訓練聯賽" value={league} onChange={event => setLeague(event.target.value)}><option value="all">全部五大聯賽</option>{LEAGUES.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><button className="button primary" disabled={blocked} onClick={() => void work.run('train',{league})}><Play size={15}/>執行訓練及驗證</button></div>
          <div className="settings-action"><div className="metric-icon mint"><Target size={20}/></div><h3>3. 更新賽前預測</h3><p>為尚未開賽的比賽生成概率，保存新的時間戳記版本；已開賽比賽不會新增預測。</p><button className="button primary" disabled={blocked} onClick={() => void work.run('predict')}><Target size={15}/>更新模型預測</button><a className="text-link" href="#/history">查看已保存預測<ExternalLink size={13}/></a></div>
        </div>
        {data.admin_configured && !adminToken && <Notice tone="warning">請先輸入管理密碼，才可啟動後端工作。</Notice>}
        {!data.admin_configured && <Notice tone="warning">未設定管理密碼時，伺服器只容許本機管理。從公開網址操作如遭拒絕，請在部署平台 Secrets 設定 ADMIN_TOKEN，再在上方輸入管理密碼。</Notice>}
        {work.error && <div role="alert"><Notice tone="warning">{work.error}</Notice></div>}
        {work.job && <div className="active-job" role="status"><div className="panel-head"><h3>{kindLabel(work.job.kind)}</h3><Badge tone={work.job.status === 'failed' || work.job.result?.errors?.length ? 'amber' : 'mint'}>{work.busy ? <LoaderCircle size={13} className="spin"/> : work.job.status === 'completed' ? <Check size={13}/> : <Clock3 size={13}/>} {labelStatus(work.job.status)}</Badge></div><p>{work.job.status === 'completed' ? `${jobResultSummary(work.job)}。頁面已重新整理，可到模型表現或數據狀態查看詳情。` : work.job.status === 'failed' ? work.job.error || '工作失敗，請查看數據狀態中的錯誤。' : '後端正在處理真實資料及模型。訓練需要等候一段時間；離開頁面不會取消已啟動的後端工作。'}</p>{(work.job.result?.errors || []).slice(0,5).map((issue: string | Bag, index: number) => <Notice tone="warning" key={index}>{typeof issue === 'string' ? issue : issue.error || '個別預測未能完成，已有資料保留。'}</Notice>)}<DetailRow label="開始時間" value={dateTime(work.job.started_at || work.job.created_at)}/>{work.job.completed_at && <DetailRow label="完成時間" value={dateTime(work.job.completed_at)}/>}</div>}
      </Panel>
      <Panel title="免費及可選 API 的設定指引" subtitle="API 金鑰只放在伺服器，不會要求你寫程式或貼出金鑰">
        <h3>預設免費來源</h3><p>Openfootball 的數據採用 CC0，可用作本項目的模型研究。不需要金鑰；社群資料可能滯後，最新收錄賽果及缺口可在「數據狀態」頁查看。</p><a className="text-link" href="https://github.com/openfootball/football.json/blob/master/LICENSE.md" target="_blank" rel="noreferrer">查看 CC0 授權<ExternalLink size={13}/></a>
        <h3>如果需要帳戶 API 的較完整賽程</h3><ol className="setup-steps"><li>前往 <a href="https://www.football-data.org/" target="_blank" rel="noreferrer">football-data.org</a> 註冊帳戶；先查看方案涵蓋的聯賽、歷史期間及額度。</li><li>閱讀官方 <a href="https://www.football-data.org/terms" target="_blank" rel="noreferrer">使用條款</a> 和 <a href="https://www.football-data.org/pricing" target="_blank" rel="noreferrer">方案價格</a>，確認顯示、保存、模型訓練及公開概率分析用途全部獲許可。本環境未能讀取官方條款及價格頁，因此未核實現行免費限制或付費金額。需要付費時，先由你決定是否批准。</li><li>在 Replit 或所用部署平台的 <strong>Secrets／環境變數</strong> 畫面新增 <code>FOOTBALL_DATA_API_KEY</code>，把帳戶提供的金鑰保存到該欄位。不要在聊天、網址或本網站的管理密碼欄輸入它。</li><li>確認以上所有用途（包括模型訓練）獲授權後新增 <code>FOOTBALL_DATA_TERMS_ACCEPTED</code>，值設為 <code>true</code>。透過平台重新啟動服務後，按上方「重新整理設定」。</li><li>選擇 football-data.org，按「同步真實資料」。若金鑰、方案權限或網絡失敗，工作記錄會清楚顯示錯誤。</li></ol>
        <Notice tone="warning">目前未取得真正 xG、xGA、已確認傷停或正選；沒有接入 API-Football 或任何付費統計來源。模型估計入球與實測 xG 會清楚分開。</Notice>
      </Panel>
      <Panel title="最近工作記錄" subtitle="即使離開頁面，已儲存的工作狀態仍可查閱"><JobsTable jobs={status.data?.jobs || []}/></Panel>
      <div className="panel-note"><CalendarDays size={14}/> 每小時同步、每日預測、數據足夠時每星期重新訓練。服務停止期間不會繼續排程；持久資料庫需由部署平台保存。</div>
    </>}
  </>;
}
