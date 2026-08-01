import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    output: str
    error: str
    exit_code: int


class HermesCliAdapterV1:
    adapter_version = "hermes-cli-v1"

    def __init__(self, binary: str, timeout: int = 180):
        self.binary = binary
        self.timeout = timeout

    def detect_version(self) -> str:
        result = subprocess.run([self.binary, "--version"], text=True, capture_output=True, timeout=15, check=False)
        if result.returncode:
            raise RuntimeError(f"Hermes version probe failed: {result.stderr.strip()}")
        return result.stdout.splitlines()[0].strip()

    def probe(self) -> dict:
        try:
            result = subprocess.run([self.binary, "--version"], capture_output=True, text=True, timeout=10, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return {"adapter_version": self.adapter_version, "available": False, "supports_profiles": False, "version": "", "error": str(exc)}
        version = (result.stdout or result.stderr).strip()[:500]
        return {"adapter_version": self.adapter_version, "available": result.returncode == 0, "supports_profiles": True, "version": version, "error": "" if result.returncode == 0 else version}

    def execute(self, profile: str, prompt: str) -> ExecutionResult:
        command = [self.binary, "--profile", profile, "-z", prompt]
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult("failed", exc.stdout or "", f"Hermes task timed out after {self.timeout}s", 124)
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        embedded_error = any(marker in stdout.lower() for marker in (
            "api call failed",
            "usage limit has been reached",
            "authentication failed",
            "provider error",
            "no available credentials",
        ))
        if result.returncode != 0 or embedded_error:
            error = stderr or stdout or f"Hermes exited with code {result.returncode}"
            return ExecutionResult("failed", "", error, result.returncode or 1)
        return ExecutionResult("succeeded", stdout, "", 0)
