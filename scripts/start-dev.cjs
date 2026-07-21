const { execFile, spawn } = require("node:child_process");
const net = require("node:net");
const { promisify } = require("node:util");
const killPort = require("kill-port");

const execFileAsync = promisify(execFile);
const args = process.argv.slice(2);
const portFlagIndex = args.indexOf("--port");
const requestedPort = portFlagIndex >= 0 ? Number(args[portFlagIndex + 1]) : 8000;

if (!Number.isInteger(requestedPort) || requestedPort < 1 || requestedPort > 65535) {
  console.error("A valid --port value is required.");
  process.exit(1);
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function isPortAvailable(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.once("error", () => resolve(false));
    server.listen({ host: "0.0.0.0", port }, () => {
      server.close(() => resolve(true));
    });
  });
}

async function releaseWindowsPort(port) {
  const command = `Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique`;
  const { stdout } = await execFileAsync("powershell.exe", [
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    command,
  ]);

  const pids = stdout
    .split(/\s+/)
    .map(Number)
    .filter((pid) => Number.isInteger(pid) && pid > 0 && pid !== process.pid);

  for (const pid of pids) {
    await execFileAsync("taskkill.exe", ["/PID", String(pid), "/T", "/F"]);
  }
}

async function releasePort(port) {
  if (await isPortAvailable(port)) return;

  if (process.platform === "win32") {
    await releaseWindowsPort(port);
  } else {
    await killPort(port, "tcp");
  }

  for (let attempt = 0; attempt < 30; attempt += 1) {
    await delay(100);
    if (await isPortAvailable(port)) return;
  }

  throw new Error(`Port ${port} is still occupied after terminating its previous listener.`);
}

async function start() {
  try {
    await releasePort(requestedPort);
  } catch (error) {
    console.error(`Unable to release port ${requestedPort}: ${error.message}`);
    process.exit(1);
  }

  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const child = spawn(
    python,
    ["-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", String(requestedPort)],
    { stdio: "inherit" },
  );

  const stop = (signal) => {
    if (!child.killed) child.kill(signal);
  };

  process.on("SIGINT", () => stop("SIGINT"));
  process.on("SIGTERM", () => stop("SIGTERM"));
  child.on("error", (error) => {
    console.error(`Unable to start Python: ${error.message}`);
    process.exit(1);
  });
  child.on("exit", (code) => process.exit(code ?? 0));
}

start();
