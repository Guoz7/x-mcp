#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/+([A-Za-z]):/, '$1:'));
const ROOT = path.resolve(HERE, '..', '..');
const STORY = path.join(HERE, 'x-mcp-200-follow-story.json');
const DECK = path.join(HERE, 'x-mcp-200-follow-deck.html');
const WORK = path.join(HERE, 'x-mcp-200-follow-work');
const DURATIONS = path.join(WORK, 'durations.json');
const OUT = path.join(HERE, 'x-mcp-200-follow.mp4');
const PAUSE = 0.8;
// Use an explicit runtime when available, otherwise resolve the local Codex
// skill from the current user's home directory. Do not commit a machine path.
const videoRuntime = process.env.X_MCP_VIDEO_RUNTIME || path.join(
  process.env.CODEX_HOME || path.join(os.homedir(), '.codex'),
  'skills', 'video-deck', 'scripts', 'package.json',
);
const require = createRequire(videoRuntime);
const { chromium } = require('playwright-core');
const ffmpeg = require('ffmpeg-static');

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { windowsHide: true });
    let stdout = '', stderr = '';
    child.stdout?.on('data', (chunk) => { stdout += chunk; });
    child.stderr?.on('data', (chunk) => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', (code) => code === 0 ? resolve({ stdout, stderr }) : reject(new Error(`${command} exited ${code}\n${stderr.slice(-3000)}`)));
  });
}

function srtTime(seconds) {
  const ms = Math.round(seconds * 1000);
  const h = Math.floor(ms / 3600000);
  const m = Math.floor(ms / 60000) % 60;
  const s = Math.floor(ms / 1000) % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')},${String(ms % 1000).padStart(3, '0')}`;
}

async function main() {
  if (!fs.existsSync(DURATIONS)) throw new Error(`Missing ${DURATIONS}; run x-mcp-200-follow-tts.ps1 first.`);
  const story = JSON.parse(fs.readFileSync(STORY, 'utf8'));
  const durations = JSON.parse(fs.readFileSync(DURATIONS, 'utf8').replace(/^\uFEFF/, ''));
  const total = durations.reduce((sum, row) => sum + Number(row.seconds), 0) + PAUSE * durations.length;
  const recDir = path.join(WORK, 'recorded');
  fs.rmSync(recDir, { recursive: true, force: true });
  fs.mkdirSync(recDir, { recursive: true });

  const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
    recordVideo: { dir: recDir, size: { width: 1920, height: 1080 } },
  });
  const page = await context.newPage();
  await page.goto(`${pathToFileURL(DECK).href}?video=1&stage=1`);
  await page.evaluate(({ durations, pause }) => {
    window.VideoDeck.play(durations.map((row) => Number(row.seconds)), pause);
  }, { durations, pause: PAUSE });
  await page.waitForTimeout(Math.ceil((total + 1.4) * 1000));
  const rawVideo = await page.video().path();
  await context.close();
  await browser.close();

  const inputs = [];
  const filters = [];
  const sequence = [];
  durations.forEach((row, index) => {
    inputs.push('-i', row.file);
    const audio = `a${index}`;
    // Input 0 is the recorded video; narration files start at input 1.
    filters.push(`[${index + 1}:a]aresample=24000[${audio}]`);
    sequence.push(`[${audio}]`);
    if (index < durations.length - 1) {
      const silence = `z${index}`;
      filters.push(`anullsrc=r=24000:cl=mono:d=${PAUSE}[${silence}]`);
      sequence.push(`[${silence}]`);
    }
  });
  filters.push(`${sequence.join('')}concat=n=${sequence.length}:v=0:a=1[full]`);
  await run(ffmpeg, [
    '-y', '-i', rawVideo, ...inputs,
    '-filter_complex', filters.join(';'),
    '-map', '0:v', '-map', '[full]',
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '20', '-pix_fmt', 'yuv420p', '-r', '30',
    '-c:a', 'aac', '-b:a', '160k', '-shortest', '-movflags', '+faststart', OUT,
  ]);

  let cursor = 0;
  const srt = [];
  const narration = [`# ${story.title}`, '', `> 6 页 | Windows SAPI | 真实数据边界版`, ''];
  durations.forEach((row, index) => {
    const slide = story.slides[index];
    const start = cursor;
    const end = cursor + Number(row.seconds);
    srt.push(`${index + 1}\n${srtTime(start)} --> ${srtTime(end)}\n${slide.narration}\n`);
    narration.push(`## ${slide.kicker || slide.title}`, `**${slide.title}**`, '', slide.narration, '');
    cursor = end + PAUSE;
  });
  fs.writeFileSync(path.join(HERE, 'x-mcp-200-follow.srt'), srt.join('\n'), 'utf8');
  fs.writeFileSync(path.join(HERE, 'x-mcp-200-follow-narration.md'), narration.join('\n'), 'utf8');
  console.log(JSON.stringify({ out: OUT, seconds: Number(total.toFixed(2)), srt: path.join(HERE, 'x-mcp-200-follow.srt') }, null, 2));
}

main().catch((error) => { console.error(error.stack || error); process.exit(1); });
