/** All editable dates represent Hong Kong time, regardless of the computer timezone. */
export function hkLocal(value:string) {
  if (!value) return '';
  const parts=value.match(/^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(\.\d{1,3})?)?(Z|[+-]\d{2}:\d{2})?)?$/);
  if(!parts) throw new Error(`日期格式不正確：${value}。請使用 YYYY-MM-DD 或 ISO 日期時間（最多三位小數秒）。`);
  const [,year,month,day,hour='00',minute='00',second='00',fraction='',zone]=parts;
  const calendar=new Date(0);calendar.setUTCFullYear(Number(year),Number(month)-1,Number(day));calendar.setUTCHours(0,0,0,0);
  if(calendar.getUTCFullYear()!==Number(year)||calendar.getUTCMonth()!==Number(month)-1||calendar.getUTCDate()!==Number(day)||Number(hour)>23||Number(minute)>59||Number(second)>59) throw new Error(`日期不存在或時間超出範圍：${value}`);
  if(!zone) return `${year}-${month}-${day}T${hour}:${minute}:${second}${fraction}`;
  const d=new Date(value); if (!Number.isFinite(d.getTime())) throw new Error(`日期格式不正確：${value}`);
  const hongKong=new Date(d.getTime()+8*3600000).toISOString();
  return hongKong.slice(0,fraction?23:19);
}
export function hkISO(value:string) { return `${value.length===16?`${value}:00`:value}+08:00`; }

/** CSV state machine supports quoted commas, quotes, CRLF, UTF-8 BOM and Excel tabs. */
export function parseCSV(text:string):string[][] {
  const clean=text.replace(/^\uFEFF/,'');
  const delimiter=clean.split(/\r?\n/,1)[0].includes('\t')?'\t':',';
  const rows:string[][]=[];let row:string[]=[];let cell='';let quoted=false;let closedQuote=false;
  for(let i=0;i<clean.length;i++) {
    const char=clean[i];
    if(char==='"') { if(quoted&&clean[i+1]==='"'){cell+='"';i++;}else if(quoted){quoted=false;closedQuote=true;}else if(!closedQuote&&!cell.trim()){quoted=true;}else throw new Error('CSV 引號格式不正確，請檢查含逗號的球隊名稱。'); }
    else if(char===delimiter&&!quoted){row.push(cell.trim());cell='';closedQuote=false;}
    else if((char==='\n'||char==='\r')&&!quoted){if(char==='\r'&&clean[i+1]==='\n')i++;row.push(cell.trim());if(row.some(Boolean))rows.push(row);row=[];cell='';closedQuote=false;}
    else if(closedQuote&&!/\s/.test(char)) throw new Error('CSV 關閉引號後有額外文字，請檢查欄位分隔。');
    else cell+=char;
  }
  if(quoted) throw new Error('CSV 有未關閉的引號，請檢查檔案。');
  row.push(cell.trim());if(row.some(Boolean))rows.push(row);
  return rows;
}
