export type Bag = Record<string, any>;
export interface Prediction {
  id: string; created_at: string; as_of: string; model_version: string;
  expected_goals: {home: number; away: number};
  probabilities: {home: number; draw: number; away: number};
  markets: {over_under: {line: number; over: number; under: number}[]; btts: {yes: number; no: number}; scores: {home: number; away: number; probability: number}[]; total_goals: {goals: number; probability: number}[]; asian_handicap: {line: number; full_win: number; half_win: number; push: number; half_loss: number; full_loss: number}[]};
  confidence: {label: string; score: number; reasons: string[]};
  data_quality: {score: number; available: string[]; missing: string[]};
  matrix: number[][]; source: string; historical: boolean;
}
export interface Match {
  id: string; league: string; season: string; home: string; away: string;
  kickoff: string; kickoff_precision: 'date' | 'exact'; status: string;
  home_goals?: number | null; away_goals?: number | null;
  source: string; source_url: string; fetched_at: string; result_available_at?: string;
  home_crest?: string; away_crest?: string; prediction?: Prediction | null;
}
export interface Team extends Bag {name: string; league: string; matches: number; elo?: number;}
export const LEAGUES = [
  {id:'PL', name:'英格蘭超級聯賽', short:'英超', country:'英格蘭', accent:'#bb90e8'},
  {id:'PD', name:'西班牙甲組聯賽', short:'西甲', country:'西班牙', accent:'#ff9e78'},
  {id:'BL1', name:'德國甲組聯賽', short:'德甲', country:'德國', accent:'#ee7986'},
  {id:'SA', name:'意大利甲組聯賽', short:'意甲', country:'意大利', accent:'#79a7f5'},
  {id:'FL1', name:'法國甲組聯賽', short:'法甲', country:'法國', accent:'#d4e773'},
];
export const leagueName = (id: string) => LEAGUES.find(x => x.id === id)?.name || id;
export const leagueShort = (id: string) => LEAGUES.find(x => x.id === id)?.short || id;
export function percent(value: unknown, digits = 1) { return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : '數據不足'; }
export function qualityPercent(value: unknown) { return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(0)}%` : '數據不足'; }
export function decimal(value: unknown, digits = 2) { return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'; }
export function count(value: unknown) { return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-HK') : '—'; }
export function dateTime(value?: string, dateOnly = false) {
  if (!value) return '尚未更新'; const d = new Date(value); if (!Number.isFinite(d.getTime())) return '時間未提供';
  return new Intl.DateTimeFormat('zh-HK',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit',...(dateOnly ? {} : {hour:'2-digit',minute:'2-digit',hour12:false})}).format(d);
}
export function matchTime(match: Match) { return match.kickoff_precision === 'date' ? `${match.kickoff.slice(0,10)} · 開賽時間未確認` : `${dateTime(match.kickoff)} HKT`; }
export function hkDate(offset = 0) { const d = new Date(Date.now() + offset * 86400000); return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Hong_Kong',year:'numeric',month:'2-digit',day:'2-digit'}).format(d); }
/** Largest remainder rounding keeps 1X2 displayed total at exactly 100.0%. */
export function roundedProbabilities(values: number[]) {
  const total = values.reduce((a,b) => a+b,0); if (!(total > 0)) return values.map(() => '—');
  const scaled = values.map(v => v / total * 1000); const parts = scaled.map(Math.floor);
  const order = scaled.map((v,i) => ({i,r:v-parts[i]})).sort((a,b) => b.r-a.r);
  const missing=1000-parts.reduce((a,b)=>a+b,0);
  for(let i=0;i<missing;i++) parts[order[i % order.length].i]++;
  return parts.map(v => `${(v / 10).toFixed(1)}%`);
}
export function labelStatus(status?: string) { return ({FINISHED:'已完場',SCHEDULED:'未開賽',TIMED:'未開賽',POSTPONED:'已延期',running:'執行中',pending:'等候中',completed:'已完成',success:'已完成',succeeded:'已完成',failed:'失敗',error:'錯誤',idle:'待命'} as Bag)[status || ''] || status || '未提供'; }
