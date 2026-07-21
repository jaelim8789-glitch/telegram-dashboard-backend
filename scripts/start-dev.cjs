const { spawn } = require("node:child_process");
const kill = require("kill-port");

const args = process.argv.slice(2);
const portFlagIndex = args.indexOf("--port");
const requestedPort = portFlagIndex >= 0 ? Number(args[portFlagIndex + 1]) : 8000;

if (!Number.isInteger(requestedPort) || requestedPort < 1 || requestedPort > 65535) {
  console.error("A valid --port value is required.");
  process.exit(1);
}

async function start() {
  try {
    await kill(requestedPort, "tcp");
    await new Promise((resolve) => setTimeout(resolve, 300));
  } catch {
    // No existing listener is the normal case.
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
