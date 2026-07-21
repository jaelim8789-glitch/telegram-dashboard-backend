const { execFile, spawn } = require("node:child_process");
const http = require("node:http");
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

function isTelemonBackendHealthy(port) {
  return new Promise((resolve) => {
    const request = http.get(
      { hostname: "127.0.0.1", port, path: "/health", timeout: 1500 },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          resolve(response.statusCode === 200 && body.includes('"status":"ok"'));
        });
      },
    );
    request.on("timeout", () => {
      request.destroy();
      resolve(false);
    });
    request.on("error", () => resolve(false));
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
    try {
      await execFileAsync("taskkill.exe", ["/PID", String(pid), "/T", "/F"]);
    } catch {
      // The process may exit between PID discovery and taskkill.
    }
  }
}

async function preparePort(port) {
  if (await isPortAvailable(port)) return "start";
  if (await isTelemonBackendHealthy(port)) return "reuse";

  if (process.platform === "win32") {
    await releaseWindowsPort(port);
  } else {
    await killPort(port, "tcp");
  }

  for (let attempt = 0; attempt < 30; attempt += 1) {
    await delay(100);
    if (await isPortAvailable(port)) return "start";
    if (await isTelemonBackendHealthy(port)) return "reuse";
  }

  throw new Error(`Port ${port} is occupied by a process that is not the Telemon backend.`);
}

function keepLauncherAlive(port) {
  console.log(`Telemon backend is already running on port ${port}; reusing it.`);
  const keepAlive = setInterval(() => {}, 60_000);
  const stop = () => {
    clearInterval(keepAlive);
    process.exit(0);
  };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
}

function startUvicorn(port) {
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  const child = spawn(
    python,
    ["-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", String(port)],
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

async function start() {
  try {
    const action = await preparePort(requestedPort);
    if (action === "reuse") {
      keepLauncherAlive(requestedPort);
      return;
    }
    startUvicorn(requestedPort);
  } catch (error) {
    console.error(`Unable to prepare port ${requestedPort}: ${error.message}`);
    process.exit(1);
  }
}

start();
