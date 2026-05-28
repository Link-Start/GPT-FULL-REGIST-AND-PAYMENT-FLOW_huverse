#!/usr/bin/env python3
"""Detachable stdlib Web UI for launching and observing full-flow runs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from full_flow_pool import FullFlowPool, PoolItem
from full_flow_proxy_pool import ProxyPool, redact_proxy


ROOT = Path(__file__).resolve().parent
DEFAULT_POOL_DB = ROOT / "accfile" / "pool" / "full_flow.sqlite3"
DEFAULT_RUNTIME_DIR = ROOT / "runtime" / "full_flow"
MAIN_SCRIPT = ROOT / "trial_payment_full_flow.py"
QUEUE_SCRIPT = ROOT / "full_flow_queue_worker.py"


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenAII 全流程控制台</title>
  <style>
    :root{--bg:#f7f8fb;--panel:#fff;--ink:#171717;--muted:#6b7280;--line:#e5e7eb;--soft:#f3f4f6;--gold:#d4af37;--blue:#2563eb;--green:#12805c;--red:#dc2626;--shadow:0 18px 48px rgba(23,23,23,.08)}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0,#fff7d8 0,#f7f8fb 34%,#f7f8fb 100%);color:var(--ink);font-family:"Plus Jakarta Sans",Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}button,input,select,textarea{font:inherit}button{cursor:pointer}
    .shell{max-width:1440px;margin:0 auto;padding:24px}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:16px}.brand h1{margin:0;font-size:28px;letter-spacing:-.04em}.brand p{margin:6px 0 0;color:var(--muted);font-size:14px}.tabs{display:flex;gap:8px;flex-wrap:wrap}.tab,.btn{border:1px solid var(--line);background:rgba(255,255,255,.76);backdrop-filter:blur(14px);border-radius:12px;padding:9px 13px;font-weight:700;color:var(--ink);transition:.18s}.tab.active,.btn.primary{background:var(--ink);border-color:var(--ink);color:#fff}.btn.gold{background:var(--gold);border-color:var(--gold);color:#151515}.btn.red{background:var(--red);border-color:var(--red);color:#fff}.btn:hover,.tab:hover{transform:translateY(-1px);box-shadow:0 10px 24px rgba(23,23,23,.08)}.btn:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid rgba(212,175,55,.38);outline-offset:2px}
    .grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:16px}.metric{background:rgba(255,255,255,.75);border:1px solid rgba(229,231,235,.9);box-shadow:var(--shadow);border-radius:18px;padding:15px}.metric span{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:800}.metric strong{display:block;margin-top:8px;font-size:30px;letter-spacing:-.04em}
    .page{display:none}.page.active{display:block}.layout{display:grid;grid-template-columns:390px minmax(0,1fr);gap:16px}.card{background:rgba(255,255,255,.82);backdrop-filter:blur(18px);border:1px solid rgba(229,231,235,.92);box-shadow:var(--shadow);border-radius:20px;padding:16px}.card h2{margin:0 0 14px;font-size:16px;letter-spacing:-.02em}.row{display:grid;gap:7px;margin-bottom:12px}.split{display:grid;grid-template-columns:1fr 1fr;gap:10px}label{font-size:12px;color:var(--muted);font-weight:800;text-transform:uppercase;letter-spacing:.06em}input,select,textarea{width:100%;border:1px solid var(--line);border-radius:12px;background:#fff;color:var(--ink);padding:10px 11px}textarea{min-height:106px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}.checks{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:8px 0 14px}.check{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:12px;padding:9px;background:#fff;font-size:13px;font-weight:700}.check input{width:auto}
    .table{overflow:auto;border:1px solid var(--line);border-radius:16px;background:#fff}.table.jobs{max-height:430px}table{width:100%;border-collapse:collapse;min-width:920px}th,td{border-bottom:1px solid #eef0f4;padding:10px 12px;text-align:left;vertical-align:top;font-size:13px}th{position:sticky;top:0;background:#fafafa;z-index:1;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}.badge{display:inline-flex;border-radius:999px;padding:3px 8px;font-weight:800;font-size:12px;background:#eef2ff;color:var(--blue)}.badge.ok{background:#dcfce7;color:var(--green)}.badge.fail{background:#fee2e2;color:var(--red)}.actions{display:flex;gap:6px;flex-wrap:wrap}.small{padding:6px 9px;border-radius:9px;font-size:12px}.status{min-height:32px;color:var(--muted);font-size:13px;display:flex;align-items:center;gap:8px}.dot{width:8px;height:8px;border-radius:999px;background:var(--gold);box-shadow:0 0 0 4px rgba(212,175,55,.18)}.log{white-space:pre-wrap;background:#101010;color:#f8f8f2;border-radius:16px;padding:14px;min-height:260px;max-height:540px;overflow:auto;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}.hide{display:none}
    @media(max-width:1100px){.grid{grid-template-columns:repeat(3,minmax(0,1fr))}.layout{grid-template-columns:1fr}}@media(max-width:640px){.shell{padding:14px}.top{display:block}.tabs{margin-top:12px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.split,.checks{grid-template-columns:1fr}}@media(prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
  </style>
</head>
<body>
  <main class="shell">
    <div class="top">
      <div class="brand"><h1>OpenAII 全流程控制台</h1><p id="dbPath">全流程任务面板</p></div>
      <nav class="tabs">
        <button class="tab active" data-page="run">运行</button>
        <button class="tab" data-page="jobs">任务</button>
        <button class="tab" data-page="pool">资源池</button>
        <button class="tab" data-page="proxy">代理池</button>
      </nav>
    </div>
    <section class="grid" id="stats"></section>

    <section class="page active" id="page-run">
      <div class="layout">
        <form class="card" id="runForm">
          <h2>启动全流程</h2>
          <div class="row"><label>邮箱</label><input name="email" autocomplete="off" placeholder="name@icloud.com"></div>
          <div class="split"><div class="row"><label>邮箱类型</label><select name="emailType"><option>auto</option><option>icloud</option><option>custom</option></select></div><div class="row"><label>验证码来源</label><select name="emailCodeProvider"><option>auto</option><option>extract_json</option><option>openai_code_json</option></select></div></div>
          <div class="row"><label>卡信息</label><input name="cardLine" autocomplete="off" placeholder="CARD MM/YY CVV"></div>
          <div class="row"><label>支付接码</label><input name="smsLine" autocomplete="off" placeholder="+1xxxx----https://sms-api"></div>
          <div class="split"><div class="row"><label>支付代理模式</label><select name="paymentMode"><option value="auto_temp">直连，t=bv 后用代理</option><option value="direct">只用直连</option><option value="force_proxy">支付开始就用代理</option></select></div><div class="row"><label>代理 ID</label><input name="paymentProxyId" type="number" min="0" placeholder="可选"></div></div>
          <div class="split"><div class="row"><label>并发数</label><input name="workers" type="number" min="1" max="12" value="1"></div><div class="row"><label>目标成功数</label><input name="successTarget" type="number" min="0" max="999" placeholder="空=不按成功数补跑"></div></div>
          <div class="split"><div class="row"><label>最大任务数</label><input name="maxRuns" type="number" min="0" max="999" placeholder="空=跑到池空"></div><div class="row"><label>补跑策略</label><input value="失败不计入目标成功数" disabled></div></div>
          <div class="split"><div class="row"><label>支付槽位</label><input name="paymentBrowserSlots" type="number" min="0" max="6" placeholder="默认配置，0=不限制"></div><div class="row"><label>说明</label><input value="协议并发，支付按槽位并发" disabled></div></div>
          <div class="checks"><label class="check"><input type="checkbox" name="sessionJson">导出 session-json cpa</label><label class="check"><input type="checkbox" name="getrt">导出 getrt cpa</label><label class="check"><input type="checkbox" name="getrtAddPhone">getrt 允许加手机号</label><label class="check"><input type="checkbox" name="usePool">使用资源池</label></div>
          <div class="row"><label>getrt 手机号接码</label><input name="getrtPhoneLine" autocomplete="off" placeholder="+1xxxx----https://sms-api"></div>
          <button class="btn primary" type="submit">启动任务</button>
          <button class="btn gold" type="button" id="queueBtn">启动资源池并发</button>
        </form>
        <section class="card">
          <div class="top" style="margin-bottom:10px"><h2>最近任务</h2><div class="actions"><button class="btn red small" id="deleteRunsBtn">删除选中</button><div class="status" id="status"><span class="dot"></span><span>就绪</span></div></div></div>
          <div class="table jobs"><table><thead><tr><th><input id="selectAllRuns" type="checkbox" aria-label="全选任务"></th><th>运行 ID</th><th>状态</th><th>耗时</th><th>支付结果</th><th>操作</th></tr></thead><tbody id="runRows"></tbody></table></div>
        </section>
      </div>
    </section>

    <section class="page" id="page-jobs">
      <div class="card">
        <div class="top" style="margin-bottom:10px"><h2>任务详情</h2><div class="actions"><select id="logFile"><option>webui.log</option><option>protocol.log</option><option>payment.log</option><option>payment_attempt2.log</option><option>getrt.log</option><option>summary.json</option></select><button class="btn small" id="refreshLogBtn">手动刷新</button></div></div>
        <div id="childRunsWrap" class="hide" style="margin-bottom:12px">
          <h2>并发子任务</h2>
          <div class="table jobs"><table><thead><tr><th>Worker</th><th>运行 ID</th><th>邮箱</th><th>状态</th><th>耗时</th><th>支付结果</th><th>操作</th></tr></thead><tbody id="childRunRows"></tbody></table></div>
        </div>
        <div class="log" id="logBox">请选择一个任务。</div>
      </div>
    </section>

    <section class="page" id="page-pool">
      <div class="layout">
        <aside class="card">
          <h2>资源池</h2>
          <div class="split"><div class="row"><label>类型</label><select id="poolKind"><option>email</option><option>card</option><option>phone</option></select></div><div class="row"><label>状态</label><select id="poolState"><option value="">全部</option><option>available</option><option>leased</option><option>failed</option></select></div></div>
          <div class="row"><label>邮箱分组</label><select id="poolBucket"><option value="">全部</option><option value="main">主池</option><option value="retry">预备/重试池</option><option value="failed">失败池</option></select></div>
          <div class="row"><label>导入数据</label><textarea id="poolValues" placeholder="一行一个"></textarea></div>
          <button class="btn primary" id="seedPoolBtn">添加到池</button>
          <button class="btn gold" id="promoteBtn">重试邮箱回主池</button>
          <button class="btn gold" id="restoreFailedBtn">失败邮箱转重试池</button>
          <button class="btn red" id="deleteFailedBtn">删除失败邮箱</button>
          <button class="btn" id="showFailedBtn">查看失败邮箱</button>
          <button class="btn gold" id="restoreSelectedPoolBtn">选中转重试</button>
          <button class="btn red" id="deleteSelectedPoolBtn">删除选中</button>
        </aside>
        <section class="card"><h2>资源列表</h2><div class="table"><table><thead><tr><th><input id="selectAllPool" type="checkbox" aria-label="全选资源"></th><th>ID</th><th>类型</th><th>值</th><th>状态</th><th>分组</th><th>使用</th><th>最近原因</th><th>关联任务</th><th>操作</th></tr></thead><tbody id="poolRows"></tbody></table></div></section>
      </div>
    </section>

    <section class="page" id="page-proxy">
      <div class="layout">
        <aside class="card">
          <h2>代理池</h2>
          <div class="row"><label>添加代理</label><textarea id="proxyValues" placeholder="host:port:user:pass(http)"></textarea></div>
          <button class="btn primary" id="addProxyBtn">添加代理</button>
        </aside>
        <section class="card"><h2>代理测试</h2><div class="table"><table><thead><tr><th>ID</th><th>代理</th><th>注册</th><th>支付</th><th>IP</th><th>最近错误</th><th>操作</th></tr></thead><tbody id="proxyRows"></tbody></table></div></section>
      </div>
    </section>
  </main>
  <script>
    const $=id=>document.getElementById(id);let selectedRun="";let logLoading=false;let lastLogKey="";let lastLogText="";
    const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    async function api(path,opt={}){const r=await fetch(path,{...opt,headers:{"content-type":"application/json",...(opt.headers||{})}});const d=await r.json().catch(()=>({}));if(!r.ok||d.ok===false)throw new Error(d.error||r.statusText);return d}
    function setStatus(t,bad=false){$("status").innerHTML=`<span class="dot"></span><span style="color:${bad?"var(--red)":"var(--muted)"}">${esc(t)}</span>`}
    function statusName(status){const m={running:"运行中",queue_finished:"并发完成",queue_failed:"并发失败",launcher_failed:"启动失败",finished_without_summary:"已结束 / 无汇总",success:"成功",payment_failed:"支付失败",protocol_failed:"协议失败",success_getrt_failed:"成功 / getrt 失败",success_session_json_failed:"成功 / session-json 失败",checkout_ready:"已产出支付链接",available:"可用",leased:"已租用",failed:"失败",ok:"可用",new:"未测"};return m[String(status||"")]||String(status||"运行中")}
    function badge(status){const s=String(status||"running");const cls=s.startsWith("success")?"ok":(s.includes("failed")?"fail":"");return `<span class="badge ${cls}">${esc(statusName(s))}</span>`}
    function bucketName(bucket){return ({main:"主池",retry:"预备/重试池",failed:"失败池"})[String(bucket||"")]||String(bucket||"")}
    function stat(label,num,hint){return `<article class="metric"><span>${esc(label)}</span><strong>${Number(num||0)}</strong><small>${esc(hint||"")}</small></article>`}
    function formData(form){return Object.fromEntries(new FormData(form).entries())}
    function activePage(){const tab=document.querySelector(".tab.active");return tab?tab.dataset.page:"run"}
    async function overview(all=false){const d=await api("/api/overview");$("dbPath").textContent=d.db;$("stats").innerHTML=[stat("活跃任务",d.activeJobs,"个任务"),stat("邮箱",d.pool.emails_main,"主池"),stat("预备邮箱",d.pool.emails_retry,"重试池"),stat("失败邮箱",d.pool.emails_failed,"待处理"),stat("卡",d.pool.cards_available,"可用"),stat("手机",d.pool.phones_available,"可用"),stat("支付代理",d.proxy.proxies_payment_ok,"可用"),stat("注册代理",d.proxy.proxies_protocol_ok,"可用")].join("");const page=activePage();if(all||page==="run")await loadRuns();if(all||page==="pool")await loadPool();if(all||page==="proxy")await loadProxies()}
    async function loadRuns(){const d=await api("/api/runs");$("runRows").innerHTML=(d.runs||[]).map(r=>{const disabled=String(r.status)==="running"?"disabled":"";const webBtn=r.hasWebLog?`<button class="btn small" onclick="showRun('${esc(r.runId)}','webui.log')">Web日志</button>`:"";return `<tr><td><input class="run-check" type="checkbox" value="${esc(r.runId)}" ${disabled}></td><td><code>${esc(r.runId)}</code></td><td>${badge(r.status)}</td><td><code>${esc(r.seconds||"")}</code></td><td>${esc(r.paymentReason||"")}</td><td><div class="actions"><button class="btn small" onclick="showRun('${esc(r.runId)}','summary.json')">汇总</button><button class="btn small" onclick="showRun('${esc(r.runId)}','payment.log')">支付日志</button>${webBtn}<button class="btn red small" onclick="deleteRuns(['${esc(r.runId)}'])" ${disabled}>删除</button></div></td></tr>`}).join("")||`<tr><td colspan="6">暂无任务</td></tr>`;$("selectAllRuns").checked=false}
    async function loadLogFiles(runId,preferred=""){if(!runId)return"";const d=await api(`/api/log-files?runId=${encodeURIComponent(runId)}`);const files=(d.files&&d.files.length?d.files:["summary.json"]);const current=preferred||$("logFile").value||files[0];$("logFile").innerHTML=files.map(f=>`<option value="${esc(f)}">${esc(f)}</option>`).join("");$("logFile").value=files.includes(current)?current:files[0];return $("logFile").value}
    async function showRun(runId,file){selectedRun=runId;lastLogKey="";lastLogText="";document.querySelector('[data-page="jobs"]').click();await loadLogFiles(runId,file);await loadQueueChildren();await loadLog(true)}
    async function loadLog(force=false){if(!selectedRun||logLoading)return;const file=$("logFile").value||await loadLogFiles(selectedRun);const key=`${selectedRun}:${file}`;const box=$("logBox");const wasNearBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-24;if(force||key!==lastLogKey)box.textContent="加载中...";logLoading=true;try{const d=await api(`/api/log?runId=${encodeURIComponent(selectedRun)}&file=${encodeURIComponent(file)}`);const text=d.text||"";if(force||key!==lastLogKey||text!==lastLogText){box.textContent=text;lastLogKey=key;lastLogText=text;if(wasNearBottom||force)box.scrollTop=box.scrollHeight}}catch(e){if(force)box.textContent=e.message||String(e)}finally{logLoading=false}}
    async function autoRefreshLog(){if(!selectedRun)return;if(!document.getElementById("page-jobs").classList.contains("active"))return;await loadLogFiles(selectedRun,$("logFile").value);await loadQueueChildren();await loadLog(false)}
    async function loadQueueChildren(){if(!selectedRun)return;try{const d=await api(`/api/queue-children?runId=${encodeURIComponent(selectedRun)}`);const children=d.children||[];if(!children.length){$("childRunsWrap").classList.add("hide");$("childRunRows").innerHTML="";return}$("childRunsWrap").classList.remove("hide");$("childRunRows").innerHTML=children.map(c=>`<tr><td><code>${esc(c.worker||"")}</code></td><td><code>${esc(c.runId)}</code></td><td>${esc(c.email||"")}</td><td>${badge(c.status)}</td><td><code>${esc(c.seconds||"")}</code></td><td>${esc(c.paymentReason||"")}</td><td><div class="actions"><button class="btn small" onclick="showRun('${esc(c.runId)}','summary.json')">汇总</button><button class="btn small" onclick="showRun('${esc(c.runId)}','protocol.log')">协议</button><button class="btn small" onclick="showRun('${esc(c.runId)}','payment.log')">支付</button></div></td></tr>`).join("")}catch(e){$("childRunsWrap").classList.add("hide")}}
    async function startRun(queue=false){const f=$("runForm");const body=formData(f);body.sessionJson=f.querySelector('[name="sessionJson"]').checked;body.getrt=f.querySelector('[name="getrt"]').checked;body.getrtAddPhone=f.querySelector('[name="getrtAddPhone"]').checked;body.usePool=f.querySelector('[name="usePool"]').checked;body.queue=queue;try{const d=await api(queue?"/api/queue/start":"/api/runs/start",{method:"POST",body:JSON.stringify(body)});setStatus(`已启动 ${d.runId}`);selectedRun=d.runId;await overview(true);await showRun(d.runId,"webui.log")}catch(e){setStatus(e.message,true)}}
    async function loadPool(){const q=new URLSearchParams({kind:$("poolKind").value,state:$("poolState").value,bucket:$("poolKind").value==="email"?$("poolBucket").value:"",limit:"300"});const d=await api(`/api/resource-items?${q}`);$("poolRows").innerHTML=(d.items||[]).map(i=>{const actions=[];const logActions=[];if(i.kind==="email"&&i.lastRunId){logActions.push(`<button class="btn small" onclick="showRun('${esc(i.lastRunId)}','summary.json')">汇总</button>`);logActions.push(`<button class="btn small" onclick="showRun('${esc(i.lastRunId)}','protocol.log')">协议</button>`);logActions.push(`<button class="btn small" onclick="showRun('${esc(i.lastRunId)}','payment.log')">支付</button>`)}if(i.kind==="email"&&(i.state==="failed"||i.bucket==="failed"))actions.push(`<button class="btn small" onclick="restoreEmail(${i.id},'retry')">转重试</button>`);if(i.kind==="email"&&i.bucket==="retry"&&i.state==="available")actions.push(`<button class="btn small" onclick="restoreEmail(${i.id},'main')">回主池</button>`);if(i.state==="leased")actions.push(`<button class="btn small" onclick="releasePool('${i.kind}',${i.id})">释放</button>`);actions.push(`<button class="btn red small" onclick="deletePool('${i.kind}',${i.id})">删除</button>`);return `<tr><td><input class="pool-check" type="checkbox" value="${i.id}" data-kind="${esc(i.kind)}" data-state="${esc(i.state)}" data-bucket="${esc(i.bucket||"")}"></td><td><code>${i.id}</code></td><td>${esc(i.kind)}</td><td><code>${esc(i.value)}</code></td><td>${badge(i.state)}</td><td>${esc(bucketName(i.bucket))}</td><td><code>${esc(i.kind==="email"?`${i.attempts||0}/${i.retryCount||0}`:i.useCount||0)}</code></td><td>${esc(i.lastReason||"")}</td><td><code>${esc(i.lastRunId||"")}</code><div class="actions">${logActions.join("")}</div></td><td><div class="actions">${actions.join("")}</div></td></tr>`}).join("")||`<tr><td colspan="10">暂无资源</td></tr>`;$("selectAllPool").checked=false}
    async function seedPool(){await api("/api/resource-seed",{method:"POST",body:JSON.stringify({kind:$("poolKind").value,values:$("poolValues").value})});$("poolValues").value="";await overview()}
    async function releasePool(kind,id){await api("/api/resource-release",{method:"POST",body:JSON.stringify({kind,id})});await overview()}
    async function deletePool(kind,id){if(!confirm("确认删除该资源？"))return;await api("/api/resource-delete",{method:"POST",body:JSON.stringify({kind,id})});await overview()}
    async function promote(){await api("/api/promote-retry-emails",{method:"POST",body:"{}"});await overview()}
    async function restoreFailed(){if(!confirm("确认将失败池中的全部邮箱转入重试池？"))return;const d=await api("/api/restore-failed-emails",{method:"POST",body:JSON.stringify({bucket:"retry"})});setStatus(`已转入重试池 ${d.restored||0} 个失败邮箱`);await overview()}
    async function deleteFailedEmails(){if(!confirm("确认删除失败池中的全部邮箱？"))return;const d=await api("/api/delete-failed-emails",{method:"POST",body:"{}"});setStatus(`已删除 ${d.deleted||0} 个失败邮箱`);await overview()}
    async function restoreEmail(id,bucket){await api("/api/resource-restore-email",{method:"POST",body:JSON.stringify({id,bucket})});await overview()}
    function selectedPoolItems(){return Array.from(document.querySelectorAll(".pool-check:checked")).map(x=>({kind:x.dataset.kind,id:Number(x.value),state:x.dataset.state,bucket:x.dataset.bucket}))}
    function togglePoolChecks(checked){document.querySelectorAll(".pool-check").forEach(x=>x.checked=checked)}
    async function restoreSelectedPool(){const items=selectedPoolItems().filter(i=>i.kind==="email");if(!items.length)return setStatus("未选择邮箱资源",true);if(!confirm(`确认将选中的 ${items.length} 个邮箱转入重试池？`))return;const d=await api("/api/resource-restore-emails",{method:"POST",body:JSON.stringify({ids:items.map(i=>i.id),bucket:"retry"})});setStatus(`已转入重试池 ${d.restored||0} 个邮箱`);await overview()}
    async function deleteSelectedPool(){const items=selectedPoolItems();if(!items.length)return setStatus("未选择资源",true);if(!confirm(`确认删除选中的 ${items.length} 个资源？`))return;const d=await api("/api/resource-delete-batch",{method:"POST",body:JSON.stringify({items})});setStatus(`已删除 ${d.deleted||0} 个资源${d.skipped&&d.skipped.length?`，跳过 ${d.skipped.length} 个`:``}`);await overview()}
    function showFailedEmails(){$("poolKind").value="email";$("poolState").value="failed";$("poolBucket").value="failed";loadPool()}
    async function loadProxies(){const d=await api("/api/proxies");$("proxyRows").innerHTML=(d.items||[]).map(p=>`<tr><td><code>${p.id}</code></td><td><code>${esc(p.redacted)}</code></td><td>${p.protocolOk?badge("ok"):badge("new")} <code>${p.protocolLatencyMs||""}</code></td><td>${p.paymentOk?badge("ok"):badge("new")} <code>${p.paymentLatencyMs||""}</code></td><td>${esc([p.ip,p.country,p.timezone].filter(Boolean).join(" / "))}</td><td>${esc(p.lastError||"")}</td><td><div class="actions"><button class="btn small" onclick="testProxy(${p.id},'protocol')">测注册</button><button class="btn small" onclick="testProxy(${p.id},'payment')">测支付</button><button class="btn red small" onclick="deleteProxy(${p.id})">删除</button></div></td></tr>`).join("")||`<tr><td colspan="7">暂无代理</td></tr>`}
    async function addProxy(){await api("/api/proxies/add",{method:"POST",body:JSON.stringify({values:$("proxyValues").value})});$("proxyValues").value="";await overview()}
    async function testProxy(id,role){setStatus(`正在测试代理 #${id} ${role}`);try{await api("/api/proxies/test",{method:"POST",body:JSON.stringify({id,role})});setStatus("代理测试完成");await overview()}catch(e){setStatus(e.message,true);await loadProxies()}}
    async function deleteProxy(id){if(!confirm("确认删除该代理？"))return;await api("/api/proxies/delete",{method:"POST",body:JSON.stringify({id})});await overview()}
    async function deleteRuns(runIds){const ids=(runIds||selectedRunIds()).filter(Boolean);if(!ids.length)return setStatus("未选择任务",true);if(!confirm(`确认删除 ${ids.length} 个历史任务？`))return;try{const d=await api("/api/runs/delete",{method:"POST",body:JSON.stringify({runIds:ids})});setStatus(`已删除 ${d.deleted.length} 个任务${d.skipped.length?`，跳过 ${d.skipped.length} 个`:``}`);if(ids.includes(selectedRun)){$("logBox").textContent="请选择一个任务。";selectedRun="";lastLogKey="";lastLogText=""}await overview()}catch(e){setStatus(e.message,true)}}
    function selectedRunIds(){return Array.from(document.querySelectorAll(".run-check:checked")).map(x=>x.value)}
    function toggleRunChecks(checked){document.querySelectorAll(".run-check:not(:disabled)").forEach(x=>x.checked=checked)}
    document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>{document.querySelectorAll(".tab,.page").forEach(x=>x.classList.remove("active"));b.classList.add("active");$(`page-${b.dataset.page}`).classList.add("active");overview()});
    $("runForm").onsubmit=e=>{e.preventDefault();startRun(false)};$("queueBtn").onclick=()=>startRun(true);$("refreshLogBtn").onclick=()=>loadLog(true);$("logFile").onchange=()=>{lastLogKey="";lastLogText="";loadLog(true)};$("seedPoolBtn").onclick=seedPool;$("promoteBtn").onclick=promote;$("restoreFailedBtn").onclick=restoreFailed;$("deleteFailedBtn").onclick=deleteFailedEmails;$("showFailedBtn").onclick=showFailedEmails;$("restoreSelectedPoolBtn").onclick=restoreSelectedPool;$("deleteSelectedPoolBtn").onclick=deleteSelectedPool;$("addProxyBtn").onclick=addProxy;$("deleteRunsBtn").onclick=()=>deleteRuns();$("selectAllRuns").onchange=e=>toggleRunChecks(e.target.checked);$("selectAllPool").onchange=e=>togglePoolChecks(e.target.checked);$("poolKind").onchange=()=>{if($("poolKind").value!=="email")$("poolBucket").value="";loadPool()};$("poolState").onchange=loadPool;$("poolBucket").onchange=loadPool;
    window.showRun=showRun;window.releasePool=releasePool;window.restoreEmail=restoreEmail;window.deletePool=deletePool;window.testProxy=testProxy;window.deleteProxy=deleteProxy;window.deleteRuns=deleteRuns;overview(true);setInterval(()=>overview(false),8000);setInterval(autoRefreshLog,1500);
  </script>
</body>
</html>"""


@dataclass
class ActiveJob:
    run_id: str
    cmd: list[str]
    log_path: Path
    out_dir: Path
    started_at: float
    proc: subprocess.Popen[str]


def load_env_file(path: Path, env: dict[str, str]) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            env.setdefault(key, value)


def now_id(prefix: str = "web") -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{stamp}"


def pool_item_to_dict(item: PoolItem | None, kind: str) -> dict[str, Any] | None:
    if not item:
        return None
    return {
        "id": item.id,
        "kind": kind,
        "value": item.value,
        "bucket": item.bucket,
        "attempts": item.attempts,
        "retryCount": item.retry_count,
        "leaseId": item.lease_id,
        "leaseUntil": item.lease_until,
    }


def read_text_tail(path: Path, max_chars: int = 60000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


class FlowWebHandler(BaseHTTPRequestHandler):
    server_version = "FullFlowWeb/1.0"

    @property
    def pool(self) -> FullFlowPool:
        return self.server.pool  # type: ignore[attr-defined, no-any-return]

    @property
    def proxies(self) -> ProxyPool:
        return self.server.proxies  # type: ignore[attr-defined, no-any-return]

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):  # type: ignore[attr-defined]
            return
        super().log_message(fmt, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                self._send_html(HTML)
                return
            if parsed.path == "/api/health":
                self._send_json({"ok": True, "db": str(self.pool.path)})
                return
            if parsed.path == "/api/overview":
                self._send_json(self._overview())
                return
            if parsed.path == "/api/stats":
                self._send_json({"ok": True, "db": str(self.pool.path), "stats": self.pool.stats()})
                return
            if parsed.path == "/api/runs":
                self._send_json({"ok": True, "runs": self._runs()})
                return
            if parsed.path == "/api/queue-children":
                query = parse_qs(parsed.query)
                self._send_json({"ok": True, "children": self._queue_children(self._query(query, "runId", ""))})
                return
            if parsed.path == "/api/log":
                query = parse_qs(parsed.query)
                run_id = self._query(query, "runId", "")
                file_name = self._query(query, "file", "summary.json")
                self._send_json({"ok": True, "text": self._read_run_file(run_id, file_name)})
                return
            if parsed.path == "/api/log-files":
                query = parse_qs(parsed.query)
                run_id = self._query(query, "runId", "")
                self._send_json({"ok": True, "files": self._available_run_files(run_id)})
                return
            if parsed.path == "/api/resource-items":
                query = parse_qs(parsed.query)
                items = self.pool.list_items(
                    self._query(query, "kind", "email"),
                    state=self._query(query, "state", ""),
                    bucket=self._query(query, "bucket", ""),
                    limit=self._int(self._query(query, "limit", "200"), 200),
                )
                self._send_json({"ok": True, "items": items})
                return
            if parsed.path == "/api/items":
                query = parse_qs(parsed.query)
                items = self.pool.list_items(
                    self._query(query, "kind", "email"),
                    state=self._query(query, "state", ""),
                    bucket=self._query(query, "bucket", ""),
                    limit=self._int(self._query(query, "limit", "200"), 200),
                )
                self._send_json({"ok": True, "items": items})
                return
            if parsed.path == "/api/proxies":
                self._send_json({"ok": True, "items": self.proxies.list_items(), "stats": self.proxies.stats()})
                return
            self._send_error("not found", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/runs/start":
                job = self._start_run(body, queue=False)
                self._send_json({"ok": True, "runId": job.run_id, "cmd": self._redact_cmd(job.cmd)})
                return
            if parsed.path == "/api/queue/start":
                job = self._start_run(body, queue=True)
                self._send_json({"ok": True, "runId": job.run_id, "cmd": self._redact_cmd(job.cmd)})
                return
            if parsed.path == "/api/runs/delete":
                deleted = self._delete_runs(body.get("runIds") or [body.get("runId")])
                self._send_json({"ok": True, **deleted})
                return
            if parsed.path == "/api/resource-seed":
                inserted = self.pool.seed_values(str(body.get("kind") or ""), self._values(body.get("values")))
                self._send_json({"ok": True, "inserted": inserted, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/seed":
                inserted = self.pool.seed_values(str(body.get("kind") or ""), self._values(body.get("values")))
                self._send_json({"ok": True, "inserted": inserted, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/resource-release":
                self.pool.release_item(str(body.get("kind") or ""), self._int(body.get("id"), 0), reason="web_release")
                self._send_json({"ok": True, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/release":
                self.pool.release_item(str(body.get("kind") or ""), self._int(body.get("id"), 0), reason=str(body.get("reason") or "web_release"))
                self._send_json({"ok": True, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/resource-delete":
                self.pool.delete_item(str(body.get("kind") or ""), self._int(body.get("id"), 0))
                self._send_json({"ok": True, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/resource-delete-batch":
                deleted = self._delete_resource_batch(body.get("items") or [])
                self._send_json({"ok": True, **deleted, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/delete":
                self.pool.delete_item(str(body.get("kind") or ""), self._int(body.get("id"), 0))
                self._send_json({"ok": True, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/acquire":
                kind = str(body.get("kind") or "email")
                item = self.pool.acquire_item(kind)
                self._send_json({"ok": True, "item": pool_item_to_dict(item, kind), "stats": self.pool.stats()})
                return
            if parsed.path == "/api/promote-retry-emails":
                promoted = self.pool.promote_retry_emails()
                self._send_json({"ok": True, "promoted": promoted, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/restore-failed-emails":
                restored = self.pool.restore_failed_emails(bucket=str(body.get("bucket") or "retry"))
                self._send_json({"ok": True, "restored": restored, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/delete-failed-emails":
                deleted = self.pool.delete_failed_emails()
                self._send_json({"ok": True, "deleted": deleted, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/resource-restore-email":
                self.pool.restore_email(self._int(body.get("id"), 0), bucket=str(body.get("bucket") or "retry"))
                self._send_json({"ok": True, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/resource-restore-emails":
                restored = self._restore_email_batch(body.get("ids") or [], bucket=str(body.get("bucket") or "retry"))
                self._send_json({"ok": True, "restored": restored, "stats": self.pool.stats()})
                return
            if parsed.path == "/api/proxies/add":
                inserted = self.proxies.add_values(self._values(body.get("values")))
                self._send_json({"ok": True, "inserted": inserted, "stats": self.proxies.stats()})
                return
            if parsed.path == "/api/proxies/delete":
                self.proxies.delete_item(self._int(body.get("id"), 0))
                self._send_json({"ok": True, "stats": self.proxies.stats()})
                return
            if parsed.path == "/api/proxies/test":
                result = self.proxies.test_item(self._int(body.get("id"), 0), str(body.get("role") or "payment"))
                self._send_json({"ok": True, "result": result})
                return
            self._send_error("not found", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error(exc)

    def _overview(self) -> dict[str, Any]:
        return {
            "ok": True,
            "db": str(self.pool.path),
            "activeJobs": self._active_job_count(),
            "pool": self.pool.stats(),
            "proxy": self.proxies.stats(),
        }

    def _start_run(self, body: dict[str, Any], *, queue: bool) -> ActiveJob:
        run_id = now_id("webq" if queue else "web")
        runtime_dir = Path(getattr(self.server, "runtime_dir"))  # type: ignore[arg-type]
        runtime_dir.mkdir(parents=True, exist_ok=True)
        if queue:
            cmd = self._build_queue_cmd(body, run_id)
        else:
            cmd = self._build_single_cmd(body, run_id)
        out_dir = runtime_dir / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "webui.log"
        env = os.environ.copy()
        load_env_file(ROOT / "full_flow.env", env)
        log_file = log_path.open("a", encoding="utf-8")
        log_file.write(f"[web] started {datetime.now(timezone.utc).isoformat()}\n")
        log_file.write("[web] cmd " + " ".join(self._redact_cmd(cmd)) + "\n")
        log_file.flush()
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT, text=True)
        job = ActiveJob(run_id=run_id, cmd=cmd, log_path=log_path, out_dir=out_dir, started_at=time.time(), proc=proc)
        self._active_jobs()[run_id] = job
        threading.Thread(target=self._watch_job, args=(job, log_file), daemon=True).start()
        return job

    def _build_single_cmd(self, body: dict[str, Any], run_id: str) -> list[str]:
        cmd = [sys.executable, str(MAIN_SCRIPT), "--run-id", run_id]
        use_pool = bool(body.get("usePool"))
        if use_pool:
            self._require_pool_ready()
            cmd.extend(["--pool-db", str(self.pool.path), "--pool-worker-id", f"web-{run_id}"])
            cmd.extend(["--pool-max-email-retries", str(getattr(self.server, "max_email_retries", 3))])  # type: ignore[attr-defined]
        else:
            self._append_required(cmd, "--email", body.get("email"), "email")
            self._append_required(cmd, "--card-line", body.get("cardLine"), "card line")
            self._append_required(cmd, "--sms-line", body.get("smsLine"), "payment sms line")
        self._append_common_flow_args(cmd, body)
        return cmd

    def _build_queue_cmd(self, body: dict[str, Any], run_id: str) -> list[str]:
        workers = max(1, min(12, self._int(body.get("workers"), 1)))
        max_runs = self._int(body.get("maxRuns"), 0)
        success_target = self._int(body.get("successTarget"), 0)
        self._require_pool_ready(
            min_emails=success_target if success_target > 0 else 1,
            min_cards=success_target if success_target > 0 else 1,
            min_phones=min(workers, success_target) if success_target > 0 else 1,
        )
        cmd = [sys.executable, str(QUEUE_SCRIPT), "--pool-db", str(self.pool.path), "--workers", str(workers), "--worker-prefix", f"{run_id}_"]
        if max_runs > 0:
            cmd.extend(["--max-runs", str(max_runs)])
        if success_target > 0:
            cmd.extend(["--success-target", str(success_target)])
            cmd.extend(["--payment-retries", "0"])
        cmd.extend(["--pool-max-email-retries", str(getattr(self.server, "max_email_retries", 3))])  # type: ignore[attr-defined]
        self._append_common_flow_args(cmd, body)
        return cmd

    def _require_pool_ready(self, *, min_emails: int = 1, min_cards: int = 1, min_phones: int = 1) -> None:
        stats = self.pool.stats()
        missing: list[str] = []
        emails = int(stats.get("emails_main", 0)) + int(stats.get("emails_retry", 0))
        cards = int(stats.get("cards_available", 0))
        phones = int(stats.get("phones_available", 0))
        if emails < max(1, min_emails):
            missing.append(f"邮箱 {emails}/{max(1, min_emails)}")
        if cards < max(1, min_cards):
            missing.append(f"卡 {cards}/{max(1, min_cards)}")
        if phones < max(1, min_phones):
            missing.append(f"手机号 {phones}/{max(1, min_phones)}")
        if missing:
            raise ValueError("资源池不足，请先补充：" + "、".join(missing))

    def _append_common_flow_args(self, cmd: list[str], body: dict[str, Any]) -> None:
        cmd.extend(["--email-type", str(body.get("emailType") or "icloud")])
        cmd.extend(["--email-code-provider", str(body.get("emailCodeProvider") or "auto")])
        mode = str(body.get("paymentMode") or "auto_temp")
        proxy_id = self._int(body.get("paymentProxyId"), 0)
        proxy_value = self.proxies.get_value(proxy_id) if proxy_id > 0 else ""
        if mode == "direct":
            cmd.extend(["--disable-payment-temp-proxy", "--disable-payment-proxy"])
        elif mode == "force_proxy":
            if proxy_value:
                cmd.extend(["--payment-proxy", proxy_value])
            cmd.extend(["--enable-payment-proxy", "--enable-payment-temp-proxy"])
        else:
            if proxy_value:
                cmd.extend(["--payment-temp-proxy", proxy_value])
            cmd.append("--enable-payment-temp-proxy")
        if body.get("sessionJson"):
            cmd.extend(["--enable-session-json", "--session-json-format", "cpa"])
        if body.get("getrt"):
            cmd.extend(["--enable-getrt", "--getrt-output-format", "cpa"])
        if body.get("getrtAddPhone"):
            cmd.append("--enable-getrt-add-phone")
        payment_slots = self._int(body.get("paymentBrowserSlots"), -1)
        if payment_slots >= 0:
            cmd.extend(["--payment-browser-slots", str(payment_slots)])
        if str(body.get("getrtPhoneLine") or "").strip():
            cmd.extend(["--getrt-phone-line", str(body.get("getrtPhoneLine")).strip()])

    def _append_required(self, cmd: list[str], flag: str, value: Any, label: str) -> None:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{label} is required")
        cmd.extend([flag, text])

    def _watch_job(self, job: ActiveJob, log_file: Any) -> None:
        rc = job.proc.wait()
        log_file.write(f"\n[web] finished rc={rc} at {datetime.now(timezone.utc).isoformat()}\n")
        log_file.flush()
        self._write_launcher_summary_if_missing(job, rc)
        log_file.close()

    def _write_launcher_summary_if_missing(self, job: ActiveJob, rc: int) -> None:
        summary_path = job.out_dir / "summary.json"
        if summary_path.exists():
            return
        is_queue = any(Path(item).name == QUEUE_SCRIPT.name for item in job.cmd)
        summary = {
            "runId": job.run_id,
            "status": ("queue_failed" if rc else "queue_finished") if is_queue else ("launcher_failed" if rc else "finished_without_summary"),
            "returnCode": rc,
            "reason": "queue worker finished; see child runs" if is_queue else "orchestrator exited before writing summary.json",
            "webLog": str(job.log_path),
            "startedAt": datetime.fromtimestamp(job.started_at, timezone.utc).isoformat(),
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "seconds": round(time.time() - job.started_at, 3),
        }
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _delete_runs(self, values: Any) -> dict[str, list[dict[str, str]] | list[str]]:
        run_ids = [str(item or "").strip() for item in values] if isinstance(values, list) else [str(values or "").strip()]
        runtime_dir = Path(getattr(self.server, "runtime_dir")).resolve()  # type: ignore[arg-type]
        active = self._active_jobs()
        deleted: list[str] = []
        skipped: list[dict[str, str]] = []
        for run_id in run_ids:
            if not run_id:
                continue
            if "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
                skipped.append({"runId": run_id, "reason": "invalid_run_id"})
                continue
            job = active.get(run_id)
            if job and job.proc.poll() is None:
                skipped.append({"runId": run_id, "reason": "running"})
                continue
            active.pop(run_id, None)
            run_dir = (runtime_dir / run_id).resolve()
            if runtime_dir not in run_dir.parents:
                skipped.append({"runId": run_id, "reason": "invalid_path"})
                continue
            if not run_dir.exists():
                skipped.append({"runId": run_id, "reason": "not_found"})
                continue
            shutil.rmtree(run_dir)
            deleted.append(run_id)
        return {"deleted": deleted, "skipped": skipped}

    def _delete_resource_batch(self, values: Any) -> dict[str, list[dict[str, str]] | int]:
        items = values if isinstance(values, list) else []
        deleted = 0
        skipped: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                skipped.append({"id": "", "kind": "", "reason": "invalid_item"})
                continue
            kind = str(item.get("kind") or "").strip()
            item_id = self._int(item.get("id"), 0)
            if kind not in {"email", "card", "phone"} or item_id <= 0:
                skipped.append({"id": str(item_id), "kind": kind, "reason": "invalid_item"})
                continue
            try:
                self.pool.delete_item(kind, item_id)
                deleted += 1
            except Exception as exc:
                skipped.append({"id": str(item_id), "kind": kind, "reason": str(exc)})
        return {"deleted": deleted, "skipped": skipped}

    def _restore_email_batch(self, values: Any, *, bucket: str = "retry") -> int:
        ids = values if isinstance(values, list) else []
        restored = 0
        for value in ids:
            item_id = self._int(value, 0)
            if item_id <= 0:
                continue
            self.pool.restore_email(item_id, bucket=bucket)
            restored += 1
        return restored

    def _runs(self) -> list[dict[str, Any]]:
        active = self._active_jobs()
        rows: list[dict[str, Any]] = []
        for run_id, job in list(active.items()):
            rc = job.proc.poll()
            if rc is not None:
                active.pop(run_id, None)
                continue
            rows.append(
                {
                    "runId": run_id,
                    "status": "running",
                    "seconds": round(time.time() - job.started_at, 1),
                    "paymentReason": "",
                    "hasWebLog": True,
                    "_mtime": time.time(),
                }
            )
        runtime_dir = Path(getattr(self.server, "runtime_dir"))  # type: ignore[arg-type]
        for summary_path in sorted(runtime_dir.glob("*/summary.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
            try:
                data = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            result = data.get("payment", {}).get("result", {}) if isinstance(data.get("payment"), dict) else {}
            rows.append(
                {
                    "runId": str(data.get("runId") or summary_path.parent.name),
                    "status": str(data.get("status") or ""),
                    "seconds": data.get("seconds", ""),
                    "paymentReason": str(result.get("reason") or ""),
                    "hasWebLog": (summary_path.parent / "webui.log").exists(),
                    "_mtime": summary_path.stat().st_mtime,
                }
            )
        for webui_path in sorted(runtime_dir.glob("*/webui.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
            if (webui_path.parent / "summary.json").exists():
                continue
            rows.append(self._webui_log_row(webui_path))
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda item: float(item.get("_mtime") or 0.0), reverse=True):
            if row["runId"] in seen:
                continue
            seen.add(row["runId"])
            row.pop("_mtime", None)
            unique.append(row)
        return unique[:40]

    def _webui_log_row(self, webui_path: Path) -> dict[str, Any]:
        text = read_text_tail(webui_path, 8000)
        rc = ""
        reason = ""
        for line in reversed(text.splitlines()):
            if not reason and "error:" in line:
                reason = line.split("error:", 1)[1].strip()
            if line.startswith("[web] finished rc="):
                rc = line.split("rc=", 1)[1].split()[0]
            if rc and reason:
                break
        status = "running" if not rc else ("finished_without_summary" if rc == "0" else "launcher_failed")
        return {
            "runId": webui_path.parent.name,
            "status": status,
            "seconds": "",
            "paymentReason": reason,
            "hasWebLog": True,
            "_mtime": webui_path.stat().st_mtime,
        }

    def _queue_children(self, run_id: str) -> list[dict[str, Any]]:
        if not self._safe_run_id(run_id):
            return []
        runtime_dir = Path(getattr(self.server, "runtime_dir")).resolve()  # type: ignore[arg-type]
        parent_dir = (runtime_dir / run_id).resolve()
        if runtime_dir not in parent_dir.parents:
            return []
        log_path = parent_dir / "webui.log"
        children: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        if log_path.exists():
            for line in read_text_tail(log_path, 200000).splitlines():
                if "[queue] child_start " in line:
                    child_id = self._kv(line, "runId")
                    if not self._safe_run_id(child_id):
                        continue
                    if child_id not in children:
                        order.append(child_id)
                    children[child_id] = {
                        **children.get(child_id, {}),
                        "runId": child_id,
                        "worker": self._kv(line, "worker"),
                        "status": "running",
                    }
                elif "[queue] child_done " in line:
                    child_id = self._kv(line, "runId")
                    if not self._safe_run_id(child_id):
                        continue
                    if child_id not in children:
                        order.append(child_id)
                    rc = self._kv(line, "rc")
                    children[child_id] = {
                        **children.get(child_id, {}),
                        "runId": child_id,
                        "worker": self._kv(line, "worker"),
                        "returnCode": rc,
                        "status": "finished_without_summary" if rc == "0" else "launcher_failed",
                    }
        for summary_path in sorted(runtime_dir.glob(f"{run_id}_*/summary.json"), key=lambda p: p.stat().st_mtime):
            child_id = summary_path.parent.name
            if not self._safe_run_id(child_id):
                continue
            if child_id not in children:
                order.append(child_id)
                children[child_id] = {"runId": child_id, "worker": ""}
        rows: list[dict[str, Any]] = []
        for child_id in order:
            row = {
                "runId": child_id,
                "worker": children[child_id].get("worker", ""),
                "status": children[child_id].get("status", "running"),
                "seconds": "",
                "email": "",
                "paymentReason": "",
                "returnCode": children[child_id].get("returnCode", ""),
                "hasWebLog": (runtime_dir / child_id / "webui.log").exists(),
            }
            summary_path = runtime_dir / child_id / "summary.json"
            if summary_path.exists():
                try:
                    data = json.loads(summary_path.read_text(encoding="utf-8"))
                    result = data.get("payment", {}).get("result", {}) if isinstance(data.get("payment"), dict) else {}
                    protocol = data.get("protocol", {}) if isinstance(data.get("protocol"), dict) else {}
                    row.update(
                        {
                            "status": str(data.get("status") or row["status"]),
                            "seconds": data.get("seconds", ""),
                            "email": str(data.get("successEmail") or protocol.get("email") or ""),
                            "paymentReason": str(result.get("reason") or data.get("reason") or ""),
                        }
                    )
                except Exception:
                    pass
            rows.append(row)
        return rows

    def _kv(self, line: str, key: str) -> str:
        prefix = key + "="
        for part in line.split():
            if part.startswith(prefix):
                return part.split("=", 1)[1].strip()
        return ""

    def _safe_run_id(self, run_id: str) -> bool:
        text = str(run_id or "").strip()
        return bool(text and "/" not in text and "\\" not in text and text not in {".", ".."})

    def _active_job_count(self) -> int:
        active = self._active_jobs()
        count = 0
        for run_id, job in list(active.items()):
            if job.proc.poll() is None:
                count += 1
            else:
                active.pop(run_id, None)
        return count

    def _read_run_file(self, run_id: str, file_name: str) -> str:
        if not run_id:
            raise ValueError("runId is required")
        safe_name = Path(file_name).name
        runtime_dir = Path(getattr(self.server, "runtime_dir"))  # type: ignore[arg-type]
        run_dir = (runtime_dir / run_id).resolve()
        if runtime_dir.resolve() not in run_dir.parents and run_dir != runtime_dir.resolve():
            raise ValueError("invalid runId")
        path = run_dir / safe_name
        if not path.exists():
            available = [
                name
                for name in (
                    "summary.json",
                    "protocol.log",
                    "payment.log",
                    "payment_attempt2.log",
                    "payment_result.json",
                    "getrt.log",
                    "webui.log",
                )
                if (run_dir / name).exists()
            ]
            available_text = "、".join(available) if available else "无"
            if safe_name == "webui.log":
                return (
                    "该任务没有 webui.log。\n"
                    "原因：webui.log 只存在于 Web 启动器父任务；队列子任务或 CLI 直接运行的任务只会生成自身阶段日志。\n"
                    f"可用日志：{available_text}\n"
                )
            return f"{safe_name} not found\n可用日志：{available_text}\n"
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-60000:]

    def _available_run_files(self, run_id: str) -> list[str]:
        if not self._safe_run_id(run_id):
            return []
        runtime_dir = Path(getattr(self.server, "runtime_dir")).resolve()  # type: ignore[arg-type]
        run_dir = (runtime_dir / run_id).resolve()
        if runtime_dir not in run_dir.parents or not run_dir.exists():
            return []
        priority = [
            "webui.log",
            "summary.json",
            "protocol.log",
            "payment.log",
            "payment_attempt2.log",
            "payment_result.json",
            "getrt.log",
            "getrt_result.json",
            "web_session_result.json",
        ]
        names = {path.name for path in run_dir.iterdir() if path.is_file() and path.suffix in {".log", ".json", ".jsonl"}}
        ordered = [name for name in priority if name in names]
        ordered.extend(sorted(name for name in names if name not in set(ordered)))
        return ordered

    def _active_jobs(self) -> dict[str, ActiveJob]:
        return self.server.active_jobs  # type: ignore[attr-defined, no-any-return]

    def _redact_cmd(self, cmd: list[str]) -> list[str]:
        redacted: list[str] = []
        redact_next = ""
        for item in cmd:
            if redact_next == "proxy":
                redacted.append(redact_proxy(item))
                redact_next = ""
                continue
            if redact_next == "secret":
                redacted.append("***")
                redact_next = ""
                continue
            redacted.append(item)
            if item in {"--payment-proxy", "--payment-temp-proxy", "--proxy-chain-upstream"}:
                redact_next = "proxy"
            elif item in {"--card-line", "--sms-line", "--getrt-phone-line"}:
                redact_next = "secret"
        return redacted

    def _read_json(self) -> dict[str, Any]:
        length = self._int(self.headers.get("content-length"), 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("request body too large")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("json object body required")
        return data

    def _values(self, value: Any) -> list[str]:
        if isinstance(value, list):
            values = [str(item).strip() for item in value]
        else:
            values = [line.strip() for line in str(value or "").splitlines()]
        return [value for value in values if value and not value.startswith("#")]

    def _query(self, query: dict[str, list[str]], key: str, default: str) -> str:
        values = query.get(key)
        return str(values[0]).strip() if values else default

    def _int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _send_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, error: Any, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"ok": False, "error": str(error)}, status)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the full-flow web console.")
    parser.add_argument("--pool-db", default=os.environ.get("FULL_FLOW_POOL_DB", str(DEFAULT_POOL_DB)))
    parser.add_argument("--host", default=os.environ.get("FULL_FLOW_WEB_HOST", os.environ.get("FULL_FLOW_POOL_WEB_HOST", "0.0.0.0")))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FULL_FLOW_WEB_PORT", os.environ.get("FULL_FLOW_POOL_WEB_PORT", "8765"))))
    parser.add_argument("--runtime-dir", default=os.environ.get("FULL_FLOW_RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR)))
    parser.add_argument("--lease-seconds", type=int, default=int(os.environ.get("FULL_FLOW_POOL_LEASE_SECONDS", "7200")))
    parser.add_argument("--max-email-retries", type=int, default=int(os.environ.get("FULL_FLOW_POOL_MAX_EMAIL_RETRIES", "3")))
    parser.add_argument("--quiet", action="store_true")
    return parser


def main() -> int:
    env: dict[str, str] = {}
    load_env_file(ROOT / "full_flow.env", env)
    for key, value in env.items():
        os.environ.setdefault(key, value)
    args = build_parser().parse_args()
    db_path = Path(args.pool_db).expanduser().resolve()
    pool = FullFlowPool(
        db_path,
        lease_seconds=max(60, int(args.lease_seconds)),
        worker_id="flow-web",
        max_email_retries=int(args.max_email_retries),
    )
    proxies = ProxyPool(db_path)
    server = ThreadingHTTPServer((args.host, int(args.port)), FlowWebHandler)
    server.pool = pool  # type: ignore[attr-defined]
    server.proxies = proxies  # type: ignore[attr-defined]
    server.runtime_dir = Path(args.runtime_dir).expanduser().resolve()  # type: ignore[attr-defined]
    server.max_email_retries = int(args.max_email_retries)  # type: ignore[attr-defined]
    server.active_jobs = {}  # type: ignore[attr-defined]
    server.quiet = bool(args.quiet)  # type: ignore[attr-defined]
    print(f"[flow-web] serving http://{args.host}:{args.port} db={pool.path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[flow-web] stopped", flush=True)
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
