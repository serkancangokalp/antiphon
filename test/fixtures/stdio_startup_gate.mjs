// Hold real filesystem startup while the parent closes stdin. Observation must
// not install an EOF listener: doing so could conceal a missing product handler.
import fs from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { setTimeout as delay } from "node:timers/promises";

const root = process.env.ANTIPHON_STDIO_TEST_GATE;
if (!root) throw new Error("stdio startup fixture requires its isolated gate");
const emit = process.stdin.emit;
process.stdin.emit = function (event, ...args) {
  if (event === "end" || event === "close") {
    fs.writeFileSync(`${root}/${event}`, "");
  }
  return emit.call(this, event, ...args);
};

async function waitForFile(name) {
  const deadline = Date.now() + 30_000;
  while (!fs.existsSync(`${root}/${name}`)) {
    if (Date.now() > deadline) throw new Error(`startup gate timed out: ${name}`);
    await delay(10);
  }
}

if (process.env.ANTIPHON_STDIO_TEST_MODE === "already-ended") {
  // Exercise the state check, independently of the new event listeners.
  process.stdin.resume();
  await waitForFile("close");
} else {
  const mkdir = fs.promises.mkdir;
  fs.promises.mkdir = async function (...args) {
    const result = await mkdir.apply(this, args);
    fs.writeFileSync(`${root}/entered`, "");
    await waitForFile("release");
    return result;
  };
  syncBuiltinESMExports();
}
