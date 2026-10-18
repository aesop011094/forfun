import { useEffect, useState } from 'react';
import { Activity, ArrowRight, BarChart3, Bell, CalendarDays, ChevronDown, ChevronRight, CircleHelp, Database, History, LayoutDashboard, Menu, PencilLine, Settings2, ShieldCheck, Users, X, Zap } from 'lucide-react';
import { useData } from './api';
import type { Bag } from './types';
import { dateTime } from './types';
import { Badge, Notice } from './shared';
import { Dashboard, MatchDetail, TeamsPage, HistoryPage, PerformancePage } from './pages';
import { DataStatusPage, SettingsPage } from './admin';
import { ManualPage } from './manual';

const navigation = [
  {path:'/',title:'比賽總覽',icon:LayoutDashboard},
  {path:'/teams',title:'球隊數據',icon:Users},
  {path:'/manual',title:'手動分析',icon:PencilLine},
  {path:'/history',title:'歷史預測',icon:History},
  {path:'/performance',title:'模型表現',icon:BarChart3},
  {path:'/data',title:'數據狀態',icon:Database},
  {path:'/settings',title:'管理設定',icon:Settings2},
];
function readRoute(){return window.location.hash.slice(1)||'/';}
export default function App(){
  const [route,setRoute]=useState(readRoute); const [mobileMenu,setMobileMenu]=useState(false); const [revision,setRevision]=useState(0); const [adminToken,setAdminToken]=useState('');
  const status=useData<Bag>('/api/status',revision);
  useEffect(()=>{const handler=()=>{setRoute(readRoute());setMobileMenu(false);window.scrollTo({top:0});};window.addEventListener('hashchange',handler);return()=>window.removeEventListener('hashchange',handler);},[]);
  useEffect(()=>{const timer=window.setInterval(()=>setRevision(x=>x+1),60000);return()=>window.clearInterval(timer);},[]);
  const path=route.split('?')[0]; const current=navigation.find(n=>n.path==='/'?path==='/'||path.startsWith('/matches/'):path.startsWith(n.path));
  const pageProps={revision,refresh:()=>setRevision(x=>x+1)};
  const sourceIsCommunity=(status.data?.sources||[]).some((x:Bag)=>String(x.name||x.provider||'').toLowerCase().includes('openfootball'));
  const freshness=status.data?.freshness||status.data?.data_freshness||{};
  const lastResult=freshness.last_result_at||status.data?.last_result_at||status.data?.latest_result_date;
  const stale=typeof freshness.stale==='boolean'?freshness.stale:lastResult?Date.now()-new Date(lastResult).getTime()>7*86400000:false;
  return <div className="app-shell">
    {mobileMenu&&<button className="sidebar-scrim" aria-label="關閉選單" onClick={()=>setMobileMenu(false)}/>}
    <aside className={`sidebar ${mobileMenu?'open':''}`}>
      <a href="#/" className="brand"><div className="brand-mark"><Zap size={24} strokeWidth={2.7}/></div><div><strong>Football<span>Intelligence</span></strong><small>AI ANALYTICS PLATFORM</small></div><button className="mobile-close" aria-label="關閉選單" onClick={e=>{e.preventDefault();setMobileMenu(false);}}><X size={19}/></button></a>
      <div className="workspace-label">工作空間 <ChevronDown size={12}/></div>
      <div className="workspace-card"><div className="workspace-icon"><Activity size={16}/></div><div><strong>足球情報中心</strong><span>五大歐洲聯賽</span></div><Badge tone="mint">研究版</Badge></div>
      <div className="nav-label">分析平台</div>
      <nav aria-label="主要導覽">{navigation.map(({path:href,title,icon:Icon})=><a href={`#${href}`} key={href} className={current?.path===href?'active':''}><Icon size={19}/><span>{title}</span>{current?.path===href&&<span className="nav-active-dot"/>}</a>)}</nav>
      <div className="sidebar-bottom"><div className="sidebar-note"><ShieldCheck size={22}/><h3>讓每個預測都有依據</h3><p>真實資料、時間序列驗證，<br/>不承諾任何固定命中率。</p><a href="#/performance">查看模型驗證<ArrowRight size={14}/></a></div><a className="sidebar-help" href="#/settings"><CircleHelp size={17}/>使用及數據說明<ChevronRight size={14}/></a><div className="sidebar-status"><span className={`status-dot ${status.error?'error':''}`}/><span>{status.error?'數據服務暫時未連線':'本地分析服務'}</span><small>v1.0</small></div></div>
    </aside>
    <div className="main-shell"><header className="topbar"><div className="breadcrumbs"><button className="menu-button" onClick={()=>setMobileMenu(true)} aria-label="開啟主要選單"><Menu size={20}/></button><span className="breadcrumb-home">分析平台</span><ChevronRight size={13}/><strong>{path.startsWith('/matches/')?'比賽詳細分析':current?.title||'比賽總覽'}</strong></div><div className="topbar-right"><span className="timezone"><CalendarDays size={14}/>香港時間 HKT</span><a className="icon-button" href="#/data" aria-label="查看數據更新狀態"><Bell size={18}/>{stale&&<i/>}</a><a href="#/settings" className="profile-avatar" aria-label="管理設定">FI</a></div></header>
      <main id="main-content"><div className="research-alert"><span><span className="status-dot"/>真實數據研究平台</span><p>概率屬模型估計，並非賽果保證。數據來源及驗證限制會如實顯示。</p><a href="#/data">數據透明度<ArrowRight size={13}/></a></div>
        {(sourceIsCommunity||stale)&&<div className="global-notice"><Notice tone="warning"><strong>{stale?'資料更新較舊，預測可靠性受影響。':'目前使用開放社群資料。'}</strong> {lastResult&&`最新已收錄賽果：${dateTime(lastResult,true)}。`}賽程可能調整；日期型賽程沒有已確認開賽時間。預測僅作研究，請先查看數據完整度。</Notice></div>}
        {path==='/'?<Dashboard {...pageProps} status={status.data}/>:path.startsWith('/matches/')?<MatchDetail {...pageProps} id={decodeURIComponent(path.slice(9))}/>:path.startsWith('/teams')?<TeamsPage {...pageProps} route={route}/>:path==='/manual'?<ManualPage revision={revision} adminToken={adminToken}/>:path==='/history'?<HistoryPage {...pageProps}/>:path==='/performance'?<PerformancePage {...pageProps}/>:path==='/data'?<DataStatusPage {...pageProps} status={status.data} statusError={status.error}/>:path==='/settings'?<SettingsPage {...pageProps} adminToken={adminToken} setAdminToken={setAdminToken}/>:<div className="empty-state"><h1>找不到這個頁面</h1><a className="button primary" href="#/">返回比賽總覽</a></div>}
        <footer className="page-footer"><span>© {new Date().getFullYear()} Football Intelligence AI</span><span><ShieldCheck size={12}/>來源透明 · 概率一致 · 歷史版本保留</span><a href="#/settings">僅供足球分析研究</a></footer>
      </main>
    </div>
  </div>;
}
