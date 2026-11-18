import { test, expect } from "@playwright/test";
import { buildSync } from "esbuild";
import { resolve } from "node:path";

const projectRoot = resolve(process.env.INIT_CWD ?? process.cwd());

type BrowserResourceTest = {
  pending: Array<{ resolve: (value: unknown) => void; reject: (reason: unknown) => void }>;
  setKey: (value: string) => void;
  refresh: () => unknown;
};

test("loaded resources survive failed refreshes and ignore stale navigation responses", async ({ page }) => {
  // Exercise the actual hook in a standalone React page. No application API,
  // authentication, database or user session is involved in this test.
  const script = buildSync({
    stdin: { contents: `
      import React from 'react';
      import {createRoot} from 'react-dom/client';
      import {useResource} from './src/features/common/hooks/use-resource';
      const pending = [];
      function Harness() {
        const [key,setKey] = React.useState('first');
        const resource = useResource(() => new Promise((resolve,reject) => pending.push({resolve,reject,key})), key);
        window.resourceTest = {pending,setKey,refresh:resource.refresh};
        return <pre id="state">{JSON.stringify({key,data:resource.data,error:resource.error,loading:resource.loading})}</pre>;
      }
      createRoot(document.getElementById('root')).render(<Harness/>);
    `, resolveDir: projectRoot, absWorkingDir: projectRoot, nodePaths: [resolve(projectRoot, 'node_modules')], loader: 'tsx' },
    bundle: true, write: false, format: 'iife', jsx: 'automatic', define: { 'process.env.NODE_ENV': '"development"' },
  }).outputFiles[0].text;
  await page.setContent('<div id="root"></div>');
  await page.addScriptTag({ content: script });
  const state = page.locator('#state');
  await expect.poll(() => page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.pending.length)).toBe(1);
  await page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.pending[0].resolve('loaded first'));
  await expect(state).toContainText('loaded first');
  await page.evaluate(() => { void (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.refresh(); });
  await expect.poll(() => page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.pending.length)).toBe(2);
  await page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.pending[1].reject(new Error('background network failure')));
  await expect(state).toContainText('loaded first');
  await expect(state).toContainText('"error":""');
  await page.evaluate(() => { void (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.refresh(); });
  await expect.poll(() => page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.pending.length)).toBe(3);
  await page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.setKey('second'));
  await expect.poll(() => page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.pending.length)).toBe(4);
  await expect(state).not.toContainText('loaded first');
  await page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.pending[3].resolve('loaded second'));
  await expect(state).toContainText('loaded second');
  await page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.pending[2].resolve('stale first'));
  await expect(state).toContainText('loaded second');
  await expect(state).not.toContainText('stale first');
  await page.clock.install();
  await page.evaluate(() => { void (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.refresh(); });
  await expect.poll(() => page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.pending.length)).toBe(5);
  await page.clock.fastForward(15001);
  await expect(state).toContainText('loaded second');
  await expect(state).toContainText('"error":""');
  await page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.setKey('unavailable'));
  await expect.poll(() => page.evaluate(() => (window as Window & { resourceTest?: BrowserResourceTest }).resourceTest?.pending.length)).toBe(6);
  await page.clock.fastForward(15001);
  await expect(state).toContainText('This page took too long to load');
  await expect(state).not.toContainText('loaded second');
});
