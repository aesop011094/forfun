import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ArrowDownToLine, Calculator, ClipboardList, Database, FileUp, LoaderCircle, PencilLine, Plus, RefreshCw, ShieldCheck, Target, Trash2 } from 'lucide-react';
import type { Bag, Match, Prediction } from './types';
import { dateTime, decimal, LEAGUES, leagueName, percent, qualityPercent, roundedProbabilities } from './types';
import { Badge, DetailRow, DistributionChart, Empty, Metric, Notice, OddsSummary, PageHeading, Panel, TeamAvatar } from './shared';
import { hkLocal, hkISO, parseCSV } from './manual-input';
import './manual.css';

type Mode = 'history' | 'custom_history' | 'scenario';
interface HistoricalRow { key:string; kickoff:string; home:string; away:string; home_goals:string; away_goals:string; available_at:string; }
interface FormValues { mode:Mode; league:string; home:string; away:string; kickoff:string; source_note:string; notes:string; home_expected_goals:string; away_expected_goals:string; }
interface Analysis { id:string; created_at:string; mode:Mode; input:Bag; match:Match; prediction:Prediction; comparison:Bag; report:{title:string;text:string}[]; data_gaps:string[]; warnings:string[]; provenance:Bag; }
interface Summary { id:string; created_at:string; mode:Mode; home:string; away:string; kickoff:string; league:string; probabilities:Prediction['probabilities']; expected_goals:Prediction['expected_goals']; }
const modes:{id:Mode;title:string;description:string;icon:typeof Database}[] = [
  {id:'history',title:'使用已收錄賽果',description:'輸入對賽組合，由現有模型分析已收錄球隊。',icon:Database},
  {id:'custom_history',title:'輸入歷史賽果',description:'提供自己的賽果資料，獨立訓練今次分析。',icon:ClipboardList},
  {id:'scenario',title:'自訂入球情境',description:'輸入兩隊預期入球假設，計算各種結果概率。',icon:Calculator},
];
const modeTitle=(mode:Mode)=>modes.find(m=>m.id===mode)?.title||'手動分析';
const blankForm=():FormValues=>({mode:'history',league:'PL',home:'',away:'',kickoff:'',source_note:'',notes:'',home_expected_goals:'',away_expected_goals:''});
let rowSequence=0;
const blankRow=():HistoricalRow=>({key:String(++rowSequence),kickoff:'',home:'',away:'',home_goals:'',away_goals:'',available_at:''});

async function manualRequest<T>(path:string,adminToken:string,init?:RequestInit):Promise<T> {
  let response:Response;
  try { response=await fetch(path,{...init,headers:{'Content-Type':'application/json',...(adminToken?{'X-Admin-Token':adminToken}:{}),...init?.headers}}); }
  catch { throw new Error('未能連接本地分析服務，請確認程式仍然開啟，再重新嘗試。'); }
  let body:Bag|null=null;try {body=await response.json();}catch{/* Clear fallback for non-JSON errors. */}
  if(!response.ok) {
    if(response.status===401||response.status===403) throw new Error('未獲授權。下載版請使用 Start.cmd 開啟的本機網址；如管理員已設定密碼，請先到「管理設定」輸入管理憑證。');
    const detail=body?.detail;
    if(typeof detail==='string') throw new Error(detail);
    if(Array.isArray(detail)) throw new Error(detail.map((e:Bag)=>`${(e.loc||[]).filter((x:string)=>x!=='body').join(' → ')}：${e.msg}`).join('；'));
    throw new Error(`分析服務暫時未能完成要求（${response.status}），請稍後再試。`);
  }
  return body as T;
}

export function ManualPage({revision,adminToken}:{revision:number;adminToken:string}) {
  const [form,setForm]=useState<FormValues>(blankForm);
  const [rows,setRows]=useState<HistoricalRow[]>(()=>[blankRow()]);
  const [teamNames,setTeamNames]=useState<string[]>([]);
  const [optionsError,setOptionsError]=useState('');
  const [error,setError]=useState('');const [importError,setImportError]=useState('');const [importMessage,setImportMessage]=useState('');
  const [busy,setBusy]=useState(false);const submitting=useRef(false);const [opening,setOpening]=useState('');
  const [analysis,setAnalysis]=useState<Analysis|null>(null);
  const [summaries,setSummaries]=useState<Summary[]>([]);const [total,setTotal]=useState(0);const [page,setPage]=useState(0);
  const [listError,setListError]=useState('');const [listLoading,setListLoading]=useState(true);const [listRevision,setListRevision]=useState(0);
  const resultRef=useRef<HTMLDivElement>(null);const fileRef=useRef<HTMLInputElement>(null);
  const setField=<K extends keyof FormValues>(field:K,value:FormValues[K])=>setForm(previous=>({...previous,[field]:value}));
  const updateRow=(key:string,field:keyof HistoricalRow,value:string)=>setRows(previous=>previous.map(row=>row.key===key?{...row,[field]:value}:row));

  useEffect(()=>{
    let active=true;setOptionsError('');
    manualRequest<{teams:string[]}>(`/api/manual/options?league=${encodeURIComponent(form.league)}`,adminToken).then(data=>{if(active)setTeamNames(data.teams||[]);}).catch(()=>{if(active){setTeamNames([]);setOptionsError('球隊建議暫時未能讀取，你仍可手動輸入完整名稱。');}});
    return()=>{active=false;};
  },[form.league,adminToken,revision]);
  useEffect(()=>{
    let active=true;setListLoading(true);setListError('');
    manualRequest<{analyses:Summary[];total:number}>(`/api/manual/analyses?limit=10&offset=${page*10}`,adminToken).then(data=>{if(active){setSummaries(data.analyses);setTotal(data.total);}}).catch(e=>{if(active)setListError(e.message);}).finally(()=>{if(active)setListLoading(false);});
    return()=>{active=false;};
  },[page,adminToken,revision,listRevision]);
  useEffect(()=>{if(analysis)resultRef.current?.scrollIntoView({behavior:'smooth',block:'start'});},[analysis]);

  function restoreInputs(saved:Analysis) {
    const input=saved.input||{};
    setForm({mode:saved.mode,league:input.league||saved.match.league,home:input.home||saved.match.home,away:input.away||saved.match.away,kickoff:hkLocal(input.kickoff||saved.match.kickoff),source_note:input.source_note||'',notes:input.notes||'',home_expected_goals:input.home_expected_goals==null?'':String(input.home_expected_goals),away_expected_goals:input.away_expected_goals==null?'':String(input.away_expected_goals)});
    setRows(input.historical_matches?.length?input.historical_matches.map((row:Bag)=>({...blankRow(),kickoff:hkLocal(row.kickoff),home:row.home,away:row.away,home_goals:String(row.home_goals),away_goals:String(row.away_goals),available_at:row.available_at?hkLocal(row.available_at):''})):[blankRow()]);
  }
  async function openAnalysis(id:string) {
    if(submitting.current||opening) return;
    setOpening(id);setError('');
    try {const saved=await manualRequest<Analysis>(`/api/manual/analyses/${encodeURIComponent(id)}`,adminToken);restoreInputs(saved);setAnalysis(saved);}
    catch(e){setError((e as Error).message);}
    finally{setOpening('');}
  }
  async function submit(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();if(submitting.current||opening)return;
    setError('');setImportError('');
    if(!form.home.trim()||!form.away.trim()||!form.kickoff){setError('請填寫主隊、客隊及開賽時間。');return;}
    if(form.home.trim().toLocaleLowerCase()===form.away.trim().toLocaleLowerCase()){setError('主隊及客隊不可相同。');return;}
    const kickoff=hkISO(form.kickoff);
    if(!Number.isFinite(new Date(kickoff).getTime())){setError('請輸入有效的香港開賽日期及時間。');return;}
    if(form.mode==='custom_history'&&rows.length<30){setError(`獨立訓練至少需要 30 場歷史賽果，目前只有 ${rows.length} 場。你可匯入 CSV，或選擇「自訂入球情境」。`);return;}
    const payload:Bag={mode:form.mode,league:form.league,home:form.home.trim(),away:form.away.trim(),kickoff,source_note:form.source_note.trim(),notes:form.notes.trim()};
    if(form.mode==='custom_history') {
      for(let i=0;i<rows.length;i++) {
        const row=rows[i];
        if(!row.kickoff||!row.home.trim()||!row.away.trim()||row.home_goals===''||row.away_goals===''){setError(`歷史賽果第 ${i+1} 行未填完整。`);return;}
        if(row.home.trim().toLocaleLowerCase()===row.away.trim().toLocaleLowerCase()){setError(`歷史賽果第 ${i+1} 行的主隊及客隊不可相同。`);return;}
      }
      payload.historical_matches=rows.map(row=>({kickoff:hkISO(row.kickoff),home:row.home.trim(),away:row.away.trim(),home_goals:Number(row.home_goals),away_goals:Number(row.away_goals),...(row.available_at?{available_at:hkISO(row.available_at)}:{})}));
    }
    if(form.mode==='scenario') {
      const home=Number(form.home_expected_goals),away=Number(form.away_expected_goals);
      if(form.home_expected_goals===''||form.away_expected_goals===''||!Number.isFinite(home)||!Number.isFinite(away)||home<0||away<0||home>10||away>10){setError('請為兩隊各輸入 0 至 10 之間的預期入球假設。');return;}
      payload.home_expected_goals=home;payload.away_expected_goals=away;
    }
    submitting.current=true;setBusy(true);
    try {const saved=await manualRequest<Analysis>('/api/manual/analyses',adminToken,{method:'POST',body:JSON.stringify(payload)});setAnalysis(saved);setPage(0);setListRevision(value=>value+1);}
    catch(e){setError((e as Error).message);}
    finally{submitting.current=false;setBusy(false);}
  }
  async function importFile(file:File) {
    setImportError('');setImportMessage('');
    if(file.size>1024*1024){setImportError('CSV 檔案上限為 1 MB，請只保留需要分析的賽果。');return;}
    try {
      const parsed=parseCSV(await file.text());
      if(parsed.length<2) throw new Error('CSV 只有標題或沒有資料，請先填入真實歷史賽果。');
      const header=parsed[0].map(cell=>cell.toLowerCase());
      const required=['kickoff','home','away','home_goals','away_goals'];
      if(required.some(name=>!header.includes(name))) throw new Error('CSV 缺少必需欄位，請下載範本並保留第一行英文標題。');
      if(parsed.length-1>500) throw new Error('每次最多匯入 500 場賽果，請分開檔案。');
      const imported=parsed.slice(1).map((cells,i)=>{
        const get=(name:string)=>cells[header.indexOf(name)]||'';
        const home_goals=get('home_goals'),away_goals=get('away_goals');
        if(!get('home')||!get('away')||!get('kickoff')||!/^\d+$/.test(home_goals)||!/^\d+$/.test(away_goals)||Number(home_goals)>30||Number(away_goals)>30)throw new Error(`CSV 第 ${i+2} 行不完整，或入球數不是 0 至 30 的整數。`);
        if(get('home').length>100||get('away').length>100)throw new Error(`CSV 第 ${i+2} 行的球隊名稱超過 100 個字，請縮短名稱並保持各行一致。`);
        return {...blankRow(),kickoff:hkLocal(get('kickoff')),home:get('home'),away:get('away'),home_goals,away_goals,available_at:get('available_at')?hkLocal(get('available_at')):''};
      });
      setRows(imported);setImportMessage(`已匯入 ${imported.length} 場賽果。下方表格可逐行修改；匯入會取代原有表格。`);
    }catch(e){setImportError((e as Error).message);}
  }
  function downloadTemplate() {
    const blob=new Blob(['\uFEFFkickoff,home,away,home_goals,away_goals,available_at\r\n'],{type:'text/csv;charset=utf-8'});
    const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='手動賽果範本.csv';a.click();window.setTimeout(()=>URL.revokeObjectURL(url),1000);
  }

  return <div className="manual-page">
    <PageHeading eyebrow="YOUR MATCH WORKSPACE" title="手動分析" description="自行輸入對賽及資料，建立有來源標示、可重新查看的分析。"/>
    <Notice>每次分析都會獨立儲存。手動資料屬使用者提供、未經核實，不會混入官方數據、模型回測或已公布的準確度。</Notice>
    <form onSubmit={submit} className="manual-form">
      <fieldset className="manual-modes" disabled={busy||!!opening}><legend>選擇分析方式</legend>{modes.map(({id,title,description,icon:Icon})=><button type="button" key={id} aria-pressed={form.mode===id} className={`manual-mode ${form.mode===id?'active':''}`} onClick={()=>{setField('mode',id);setError('');}}><Icon size={21}/><span><strong>{title}</strong><small>{description}</small></span><i aria-hidden="true"/></button>)}</fieldset>
      <fieldset disabled={busy||!!opening} className="manual-fields-group">
        <Panel title="1. 輸入比賽" subtitle="以下時間一律為香港時間（HKT），與電腦設定無關">
          <div className="manual-fields">
            <label>聯賽<select value={form.league} onChange={e=>setField('league',e.target.value)}>{LEAGUES.map(league=><option key={league.id} value={league.id}>{league.name}</option>)}</select></label>
            <label>開賽日期及時間（香港）<input type="datetime-local" step="0.001" required value={form.kickoff} onChange={e=>setField('kickoff',e.target.value)}/></label>
            <label>主隊名稱<input required maxLength={100} list="manual-team-names" autoComplete="off" value={form.home} onChange={e=>setField('home',e.target.value)} placeholder="輸入或選擇主隊"/></label>
            <label>客隊名稱<input required maxLength={100} list="manual-team-names" autoComplete="off" value={form.away} onChange={e=>setField('away',e.target.value)} placeholder="輸入或選擇客隊"/></label>
          </div>
          {form.kickoff&&Number.isFinite(new Date(hkISO(form.kickoff)).getTime())&&new Date(hkISO(form.kickoff)).getTime()<=Date.now()&&<div className="manual-retrospective"><Notice tone="warning">這個開賽時間已過。分析只屬回顧研究，並非當時實際發布的賽前預測，也不會計入官方準確度。</Notice></div>}
          <datalist id="manual-team-names">{teamNames.map(name=><option value={name} key={name}/>)}</datalist>
          {optionsError&&<p className="manual-help">{optionsError}</p>}
          {form.mode==='history'&&<p className="manual-help">球隊名稱須與已收錄資料完全一致。每隊至少需 5 場、所選聯賽至少需 80 場可用賽果；不夠資料時系統會拒絕產生預測。</p>}
          {form.mode!=='history'&&<p className="manual-help">可輸入未收錄的球隊。聯賽欄位只作分類，不代表這場比賽已獲資料供應商確認。</p>}
        </Panel>
        {form.mode==='custom_history'&&<Panel title="2. 輸入歷史賽果" subtitle="至少 30 場已完場比賽，每支分析球隊至少 5 場；只供今次獨立訓練使用" action={<Badge tone="amber">未經核實</Badge>}>
          <Notice tone="warning">請提供多支球隊的聯賽賽果，以便比較對手實力。所有賽果必須在目前時間及今場開賽前已可取得。過往賽事分析只會使用當時可用的資料。沒有提供「賽果可用時間」時，系統會假設開賽 36 小時後才可使用，並清楚記錄這項限制。</Notice>
          <div className="manual-import-actions"><button type="button" className="button secondary" onClick={downloadTemplate}><ArrowDownToLine size={15}/>下載 CSV 範本</button><button type="button" className="button secondary" onClick={()=>fileRef.current?.click()}><FileUp size={15}/>匯入 CSV（取代表格）</button><span>{rows.length} / 500 場</span><input ref={fileRef} type="file" accept=".csv,.tsv,text/csv,text/tab-separated-values" aria-label="匯入歷史賽果 CSV" className="manual-file-input" onChange={e=>{const file=e.target.files?.[0];if(file)void importFile(file);e.target.value='';}}/></div>
          <details className="manual-csv-help"><summary>CSV 填寫說明</summary><p>保留範本第一行英文標題，每行一場賽果。kickoff 為開賽時間，home / away 為主客隊，home_goals / away_goals 為全場入球，available_at 可留空。日期格式為 2026-09-01T20:00+08:00；沒有時區時當作香港時間，只有日期時按當日 00:00 處理。請儲存為 UTF-8 CSV，球隊名稱含逗號時以雙引號包住。</p></details>
          {importError&&<div className="manual-error" role="alert">{importError}</div>}{importMessage&&<div className="manual-import-message" role="status">{importMessage}</div>}
          <div className="manual-history-rows">{rows.map((row,i)=><div className="manual-history-row" key={row.key}><div className="manual-row-number">賽果 {i+1}<button type="button" aria-label={`刪除賽果 ${i+1}`} onClick={()=>setRows(previous=>previous.filter(item=>item.key!==row.key))}><Trash2 size={15}/></button></div><div className="manual-row-fields"><label>開賽時間（香港）<input type="datetime-local" step="0.001" required value={row.kickoff} onChange={e=>updateRow(row.key,'kickoff',e.target.value)}/></label><label>主隊<input required maxLength={100} value={row.home} onChange={e=>updateRow(row.key,'home',e.target.value)}/></label><label>客隊<input required maxLength={100} value={row.away} onChange={e=>updateRow(row.key,'away',e.target.value)}/></label><label>主隊入球<input type="number" required min="0" max="30" step="1" inputMode="numeric" value={row.home_goals} onChange={e=>updateRow(row.key,'home_goals',e.target.value)}/></label><label>客隊入球<input type="number" required min="0" max="30" step="1" inputMode="numeric" value={row.away_goals} onChange={e=>updateRow(row.key,'away_goals',e.target.value)}/></label><label>賽果可用時間（選填，香港）<input type="datetime-local" step="0.001" value={row.available_at} onChange={e=>updateRow(row.key,'available_at',e.target.value)}/></label></div></div>)}</div>
          <button type="button" className="button secondary manual-add-row" disabled={rows.length>=500} onClick={()=>setRows(previous=>[...previous,blankRow()])}><Plus size={15}/>新增一場賽果</button>
        </Panel>}
        {form.mode==='scenario'&&<Panel title="2. 設定預期入球假設" subtitle="這是自訂情境運算，未經歷史驗證或概率校準" action={<Badge tone="amber">假設情境</Badge>}>
          <Notice tone="warning">數值由你自行假設，系統只會計算這組假設下的比分及市場概率。這不等於經訓練模型的預測，更不是真實 xG；沒有可聲稱的命中率或信心分數。</Notice>
          <div className="manual-fields"><label>主隊預期入球（假設）<input type="number" required min="0" max="10" step="0.01" inputMode="decimal" value={form.home_expected_goals} onChange={e=>setField('home_expected_goals',e.target.value)} placeholder="0 至 10"/></label><label>客隊預期入球（假設）<input type="number" required min="0" max="10" step="0.01" inputMode="decimal" value={form.away_expected_goals} onChange={e=>setField('away_expected_goals',e.target.value)} placeholder="0 至 10"/></label></div>
        </Panel>}
        <Panel title={`${form.mode==='history'?'2':'3'}. 資料來源及補充` } subtitle="記錄你參考的資料，方便日後核對">
          <div className="manual-fields"><label className="manual-full">資料來源（選填）<input value={form.source_note} maxLength={200} onChange={e=>setField('source_note',e.target.value)} placeholder="例如：資料網站名稱、網址或自己的紀錄"/></label><label className="manual-full">補充資料／傷停筆記（選填）<textarea value={form.notes} maxLength={2000} rows={3} onChange={e=>setField('notes',e.target.value)} placeholder="只填寫你已知的資料；未確認內容請清楚註明。"/></label></div>
          <p className="manual-help">文字筆記只會原樣保留，不會自動核實，也不會改動模型概率。現有模型未支援的傷停、正選及 xG，不會被包裝成已使用的分析特徵。</p>
        </Panel>
      </fieldset>
      {error&&<div className="manual-error" role="alert">{error}{error.includes('管理')&&<a href="#/settings">前往管理設定</a>}</div>}
      <div className="manual-submit"><p>每次提交都建立新的獨立紀錄，原有分析不會被覆寫。</p><button type="submit" className="button primary" disabled={busy||!!opening}>{busy?<LoaderCircle className="spin" size={17}/>:<PencilLine size={17}/>} {busy?'正在計算及儲存…':'分析並儲存'}</button></div>
    </form>
    {analysis&&<div ref={resultRef} className="manual-result" tabIndex={-1}><AnalysisResult analysis={analysis}/></div>}
    <Panel title="已儲存的手動分析" subtitle="與官方預測記錄分開保存；點擊可重新查看及載入原始輸入" action={<button type="button" className="button secondary" onClick={()=>setListRevision(value=>value+1)} disabled={listLoading}><RefreshCw size={14}/>重新整理</button>}>
      {listError?<div className="manual-error" role="alert">{listError}</div>:listLoading?<p className="manual-help" role="status">正在讀取儲存紀錄…</p>:!summaries.length?<Empty title="尚未有手動分析">完成上方表格並按「分析並儲存」，便會在這裏保留輸入、概率及時間記錄。</Empty>:<><div className="manual-saved-list">{summaries.map(saved=><button type="button" className={`manual-saved ${analysis?.id===saved.id?'active':''}`} key={saved.id} disabled={busy||!!opening} onClick={()=>void openAnalysis(saved.id)}><span className="manual-saved-match"><strong>{saved.home} <i>對</i> {saved.away}</strong><small>{leagueName(saved.league)} · 開賽 {dateTime(saved.kickoff)} HKT</small></span><span className="manual-saved-meta"><Badge tone={saved.mode==='history'?'mint':'amber'}>{modeTitle(saved.mode)}</Badge><small>儲存 {dateTime(saved.created_at)} HKT</small></span><span className="manual-saved-open">{opening===saved.id?'讀取中…':'查看分析'}</span></button>)}</div>{total>10&&<div className="pagination"><button type="button" className="button secondary" disabled={page===0||!!opening} onClick={()=>setPage(value=>value-1)}>上一頁</button><span>第 {page+1} / {Math.ceil(total/10)} 頁</span><button type="button" className="button secondary" disabled={(page+1)*10>=total||!!opening} onClick={()=>setPage(value=>value+1)}>下一頁</button></div>}</>}
    </Panel>
  </div>;
}

function AnalysisResult({analysis}:{analysis:Analysis}) {
  const {match,prediction:p,provenance,comparison}=analysis;
  const isScenario=analysis.mode==='scenario';
  const sourceURLs:string[]=analysis.mode==='history'?[...new Set([...(comparison?.home?.source_urls||[]),...(comparison?.away?.source_urls||[])].filter((value:unknown):value is string=>{if(typeof value!=='string')return false;try{return ['http:','https:'].includes(new URL(value).protocol);}catch{return false;}}))]:[];
  const reasons=[...(analysis.warnings||[]),...(analysis.data_gaps||[]),...(p.data_quality?.missing||[])].filter((value,i,all)=>all.indexOf(value)===i);
  const [showMatrix,setShowMatrix]=useState(false);
  const probabilityLabels=roundedProbabilities(p.matrix.flat());
  const comparisonRows:[string,string][]=[['可用歷史場數','matches'],['Elo 實力評分','elo'],['平均入球','goals_for_per_game'],['平均失球','goals_against_per_game'],['近 5 場積分','points_last5'],['近 10 場積分','points_last10'],['攻力倍率（高為佳）','attack_strength'],['防守失球倍率（低為佳）','defence_strength']];
  return <>
    <div className="manual-result-heading"><div><span className="eyebrow">SAVED ANALYSIS</span><h2>手動分析結果</h2><p>儲存於 {dateTime(analysis.created_at)} HKT · 修改上方輸入後，請重新按「分析並儲存」。</p></div><Badge tone="amber">{modeTitle(analysis.mode)} · 手動輸入</Badge></div>
    <Notice tone="warning">{provenance.fixture||'比賽資料由使用者輸入，未經核實。'} {isScenario?'以下是自訂假設下的數學概率，沒有經驗證的預測準確度。':'這份分析不會計入官方歷史準確度。'} {analysis.mode==='custom_history'?'手動賽果模型尚未經獨立樣本驗證，請審慎解讀。':''}</Notice>
    <section className="manual-result-match"><div><TeamAvatar name={match.home}/><strong>{match.home}</strong><small>主場</small></div><span><b>VS</b><small>{dateTime(match.kickoff)} HKT</small><small>{leagueName(match.league)}</small></span><div><TeamAvatar name={match.away}/><strong>{match.away}</strong><small>作客</small></div></section>
    <div className="detail-metrics"><Metric label={isScenario?'假設總入球':'模型預期總入球'} value={decimal(p.expected_goals.home+p.expected_goals.away)} detail="並非真實 xG" icon={<Target size={17}/>}/><Metric label="預測信心" value={isScenario?'未經驗證':p.confidence?.label||'未經驗證'} detail={isScenario?'假設情境不提供信心分數':`綜合評估 · 並非最高勝率`} icon={<ShieldCheck size={17}/>} color="purple"/><Metric label="資料完整度" value={isScenario?'自訂假設':analysis.mode==='custom_history'?'未經核實':qualityPercent(p.data_quality?.score)} detail={isScenario?'沒有引用歷史資料':analysis.mode==='custom_history'?`${provenance.history_count??'—'} 場輸入賽果，未經核實`:`${provenance.history_count??'—'} 場可用歷史賽果 · 來源見下方`} icon={<Database size={17}/>} color="blue"/></div>
    <div className="two-column"><Panel title="勝平負概率" subtitle="四捨五入後合共 100.0%"><OddsSummary prediction={p}/><div className="panel-note">版本 {p.model_version} · 資料截止 {dateTime(p.as_of)} HKT</div></Panel><Panel title={isScenario?'輸入的預期入球情境':'模型預期入球'} subtitle="由比分概率分布計算；並非實際射門質素 xG"><div className="expected-goals"><div><TeamAvatar name={match.home} size="small"/><span>{match.home}</span><b>{decimal(p.expected_goals.home)}</b></div><div><TeamAvatar name={match.away} size="small"/><span>{match.away}</span><b>{decimal(p.expected_goals.away)}</b></div></div></Panel></div>
    <div className="two-column"><Panel title="大小球及雙方入球" subtitle="與勝平負、正確比分來自同一分布"><div className="table-wrap"><table><thead><tr><th>入球線</th><th>大球</th><th>小球</th></tr></thead><tbody>{p.markets.over_under.map(row=>{const values=roundedProbabilities([row.over,row.under]);return <tr key={row.line}><td>{row.line} 球</td><td className="mint-text">{values[0]}</td><td>{values[1]}</td></tr>;})}</tbody></table></div><div className="btts-row"><div><h3>雙方入球 BTTS</h3><span>兩隊至少各入一球</span></div><div><strong>{roundedProbabilities([p.markets.btts.yes,p.markets.btts.no])[0]}</strong><span>是</span></div><div><strong>{roundedProbabilities([p.markets.btts.yes,p.markets.btts.no])[1]}</strong><span>否</span></div></div></Panel><Panel title="最可能的 5 個比分" subtitle="列出的 5 個比分不代表全部可能結果"><div className="score-list">{p.markets.scores.slice(0,5).map((score,i)=><div key={`${score.home}-${score.away}`}><span className="score-rank">0{i+1}</span><strong>{score.home}<i>:</i>{score.away}</strong><div className="score-bar"><span style={{width:`${score.probability*100}%`}}/></div><b>{percent(score.probability)}</b></div>)}</div></Panel></div>
    <Panel title="總入球概率分布" subtitle="每個數值代表該場比賽總入球的可能性"><DistributionChart values={p.markets.total_goals.map(row=>row.probability)} labels={p.markets.total_goals.map(row=>String(row.goals))}/></Panel>
    <Panel title="亞洲讓球分析" subtitle="以主隊為方向，按每個比分計算五種結算狀態"><div className="table-wrap"><table className="handicap-table"><thead><tr><th>主隊讓球</th><th>全贏</th><th>半贏</th><th>走盤</th><th>半輸</th><th>全輸</th></tr></thead><tbody>{p.markets.asian_handicap.map(row=>{const values=roundedProbabilities([row.full_win,row.half_win,row.push,row.half_loss,row.full_loss]);return <tr key={row.line}><td>{row.line>0?'+':''}{row.line.toFixed(2)}</td>{values.map((value,i)=><td key={i} className={i===0?'mint-text':i===4?'rose-text':''}>{value}</td>)}</tr>;})}</tbody></table></div><div className="panel-note">例如主隊 -0.25 拆成 0 及 -0.5；和局時半輸。每行五種狀態合計 100.0%。</div></Panel>
    {!isScenario&&<Panel title="球隊資料比較" subtitle="只根據今次資料截止時間前的可用賽果"><div className="comparison-header"><span>{match.home}</span><span>{match.away}</span></div>{comparisonRows.map(([label,key])=><div className="comparison-row" key={key}><strong>{comparison?.home?.[key]==null?'數據不足':decimal(comparison.home[key],key==='matches'||key==='elo'?0:2)}</strong><span>{label}</span><strong>{comparison?.away?.[key]==null?'數據不足':decimal(comparison.away[key],key==='matches'||key==='elo'?0:2)}</strong></div>)}<div className="panel-note">只含已輸入或已收錄的賽果；不代表所有賽事、傷停或球員狀況均已取得。</div></Panel>}
    <Panel title="繁體中文分析報告" subtitle={isScenario?'根據自訂假設及計算結果生成':'根據可用統計及模型輸出生成，不添加未核實原因'}><div className="report-grid">{analysis.report.map((section,i)=><div className="report-section" key={i}><span className="report-index">{String(i+1).padStart(2,'0')}</span><div><h3>{section.title}</h3><p>{section.text}</p></div></div>)}</div></Panel>
    <div className="two-column"><Panel title="輸入來源及原始筆記" subtitle="使用者提供，未經系統核實"><DetailRow label="分析資料" value={provenance.data_source||p.source}/><DetailRow label="資料截止時間（香港）" value={`${dateTime(p.as_of)} HKT`}/>{analysis.mode==='history'&&<><h3 className="subheading">已收錄賽果的原始來源</h3>{sourceURLs.length?<ul className="manual-limitations">{sourceURLs.map(url=><li key={url}><a href={url} target="_blank" rel="noreferrer" style={{color:'var(--mint)',textDecoration:'underline',overflowWrap:'anywhere'}}>{url}</a></li>)}</ul>:<p className="manual-help">原始資料來源連結未有提供。</p>}</>}<DetailRow label="來源說明" value={analysis.input.source_note||'未提供'}/><DetailRow label="資料可用時間" value={provenance.availability_note||'資料截止前可用'}/><DetailRow label="官方準確度統計" value="不包括今次分析"/><h3 className="subheading">原始筆記（不參與概率計算）</h3><p className="manual-raw-notes">{analysis.input.notes||'未有補充筆記。'}</p></Panel><Panel title="不確定因素及缺項" subtitle="請連同概率一起閱讀">{reasons.length?<ul className="manual-limitations">{reasons.map((reason,i)=><li key={i}>{reason}</li>)}</ul>:<p className="manual-help">手動輸入仍可能包含未發現的誤差，請自行核對原始資料。</p>}{p.confidence?.reasons?.length?<><h3 className="subheading">信心評估依據</h3><ul className="manual-limitations">{p.confidence.reasons.map((reason,i)=><li key={i}>{reason}</li>)}</ul></>:null}</Panel></div>
    <Panel title="已儲存的獨立快照" subtitle="原始輸入、模型結果及建立時間一併保存，修改後須建立新紀錄"><DetailRow label="紀錄編號" value={<code>{analysis.id}</code>}/><DetailRow label="建立時間（香港）" value={dateTime(analysis.created_at)}/><DetailRow label="模型版本" value={p.model_version}/><button type="button" className="matrix-toggle" aria-expanded={showMatrix} onClick={()=>setShowMatrix(value=>!value)}>{showMatrix?'收起':'查看'}完整比分概率矩陣</button>{showMatrix&&<div className="table-wrap manual-matrix"><table><thead><tr><th>主╲客</th>{p.matrix[0].map((_,i)=><th key={i}>{i}</th>)}</tr></thead><tbody>{p.matrix.map((row,i)=><tr key={i}><th>{i}</th>{row.map((_,j)=><td key={j}>{probabilityLabels[i*p.matrix[0].length+j]}</td>)}</tr>)}</tbody></table></div>}</Panel>
  </>;
}
