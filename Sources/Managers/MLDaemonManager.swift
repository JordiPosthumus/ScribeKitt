import Foundation
import Darwin
import os.log

internal enum MLDaemonError: Error, LocalizedError {
    case scriptNotFound
    case daemonUnavailable(String)
    case invalidResponse(String)
    case remoteError(String)
    case restartLimitReached
    case writeFailed

    var errorDescription: String? {
        switch self {
        case .scriptNotFound:
            return "ml_daemon.py could not be found"
        case .daemonUnavailable(let reason):
            return "ML daemon unavailable: \(reason)"
        case .invalidResponse(let reason):
            return "Invalid response from ML daemon: \(reason)"
        case .remoteError(let message):
            return "ML daemon error: \(message)"
        case .restartLimitReached:
            return "ML daemon restart limit reached"
        case .writeFailed:
            return "Failed to write request to ML daemon"
        }
    }
}

internal actor MLDaemonManager {
    static let shared = MLDaemonManager()

    private struct PendingRequest {
        let completion: (Result<Data, Error>) -> Void
    }

    private let logger = Logger(subsystem: "com.audiowhisper.app", category: "MLDaemon")
    private let maxRestartAttempts = 3
    private let inputQueue = DispatchQueue(label: "com.audiowhisper.daemon-input")

    private var process: Process?
    private var processGeneration: UUID?
    private var stdinPipe: Pipe?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    private var pending: [Int: PendingRequest] = [:]
    private var nextRequestID: Int = 1
    private var restartAttempts: Int = 0
    private var isShuttingDown = false
    private var recordingActive = false
    private var stdoutReaderTask: Task<Void, Never>?
    private var pythonExecutable: URL?
    private var scriptLocation: URL?
#if DEBUG
    private var testResponder: ((String, [String: Any]) throws -> Any)?
#endif

    // MARK: - Public API

    struct STTStatus: Decodable {
        let running: Bool
        let port: Int?
        let token_path: String
        let error: String?
    }

    func configureSTT(enabled: Bool, port: Int, origins: [String]) async throws -> STTStatus {
        try await sendRequest(method: "stt_configure", params: [
            "enabled": enabled, "port": port, "origins": origins
        ])
    }

    func sttStatus() async throws -> STTStatus {
        try await sendRequest(method: "stt_status", params: [:])
    }

    func setRecordingActive(_ active: Bool) async {
        recordingActive = active
        struct Result: Decodable { let success: Bool }
        let _: Result? = try? await sendRequest(method: "recording_state", params: ["active": active])
    }

    func prepareRuntime() async throws {
        struct Result: Decodable { let pong: Bool }
        let result: Result = try await sendRequest(method: "ping", params: [:])
        guard result.pong else { throw MLDaemonError.daemonUnavailable("Runtime did not respond") }
    }

    /// Called only by the explicit first-run setup action, never by transcription.
    func prepareModel() async throws {
        struct Result: Decodable { let success: Bool }
        let result: Result = try await sendRequest(method: "prepare_model", params: [:])
        guard result.success else { throw MLDaemonError.remoteError("Model setup did not finish") }
    }

    func verifySetup() async throws {
        struct Result: Decodable { let success: Bool }
        let result: Result = try await sendRequest(method: "verify_setup", params: [:])
        guard result.success else { throw MLDaemonError.remoteError("Offline model verification failed") }
    }

    struct PreviewResult: Decodable, Equatable {
        let active: Bool
        let stable: String
        let draft: String
    }

    func startPreview(sessionID: UUID) async throws {
        struct Result: Decodable { let success: Bool }
        let result: Result = try await sendRequest(method: "preview_start", params: [
            "session_id": sessionID.uuidString, "repo": ParakeetModel.v2English.rawValue
        ])
        guard result.success else { throw MLDaemonError.remoteError("Preview could not start") }
    }

    func appendPreview(sessionID: UUID, sequence: Int, pcm: Data) async throws -> PreviewResult {
        try await sendRequest(method: "preview_audio", params: [
            "session_id": sessionID.uuidString, "sequence": sequence,
            "audio_b64": pcm.base64EncodedString()
        ])
    }

    func endPreview(sessionID: UUID) async {
        struct Result: Decodable { let success: Bool }
        let _: Result? = try? await sendRequest(method: "preview_end", params: ["session_id": sessionID.uuidString])
    }

    func transcribe(repo: String, pcmPath: String) async throws -> String {
        struct TranscribeResult: Decodable { let success: Bool; let text: String; let error: String? }
        let result: TranscribeResult = try await sendRequest(
            method: "transcribe",
            params: ["repo": repo, "pcm_path": pcmPath]
        )
        guard result.success else { throw MLDaemonError.remoteError(result.error ?? "Transcription failed") }
        return result.text
    }

    func warmup(type: String, repo: String) async throws {
        struct WarmupResult: Decodable { let success: Bool; let error: String? }
        let result: WarmupResult = try await sendRequest(method: "warmup", params: ["type": type, "repo": repo])
        guard result.success else { throw MLDaemonError.remoteError(result.error ?? "Warmup failed") }
    }

    func ping() async -> Bool {
        struct PingResult: Decodable { let pong: Bool }
        do {
            let result: PingResult = try await sendRequest(method: "ping", params: [:])
            return result.pong
        } catch {
            logger.error("Ping failed: \(error.localizedDescription)")
            return false
        }
    }

    // MARK: - Core JSON-RPC plumbing

    private func sendRequest<Response: Decodable>(method: String, params: [String: Any]) async throws -> Response {
        try Task.checkCancellation()
#if DEBUG
        if let testResponder {
            let resultObject = try testResponder(method, params)
            let data = try JSONSerialization.data(withJSONObject: resultObject, options: [.fragmentsAllowed])
            return try decodeResponse(data)
        }
#endif
        try ensureDaemonRunning()

        let requestID = nextRequestID
        nextRequestID += 1

        var payload: [String: Any] = [
            "jsonrpc": "2.0",
            "id": requestID,
            "method": method
        ]
        if !params.isEmpty {
            payload["params"] = params
        }

        var data = try JSONSerialization.data(withJSONObject: payload, options: [])
        data.append(0x0a)
        guard let writer = stdinPipe?.fileHandleForWriting else {
            throw MLDaemonError.daemonUnavailable("stdin unavailable")
        }

        let response = try await waitForResponse(id: requestID, frame: data, writer: writer)
        return try decodeResponse(response)
    }

    private func decodeResponse<Response: Decodable>(_ data: Data) throws -> Response {
        do { return try JSONDecoder().decode(Response.self, from: data) }
        catch { throw MLDaemonError.invalidResponse(error.localizedDescription) }
    }

    private func waitForResponse(id: Int, frame: Data, writer: FileHandle) async throws -> Data {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                // Cancellation can arrive before the continuation is installed.
                guard !Task.isCancelled else {
                    continuation.resume(throwing: CancellationError())
                    return
                }
                pending[id] = PendingRequest { continuation.resume(with: $0) }
                // A busy daemon may fill stdin. Keep blocking pipe writes off the actor,
                // while serializing whole frames so concurrent requests cannot interleave.
                inputQueue.async { [weak self] in
                    do {
                        try writer.write(contentsOf: frame)
                    } catch {
                        Task { await self?.failWrite(id) }
                    }
                }
            }
        } onCancel: {
            Task { await self.cancelRequest(id) }
        }
    }

    private func failWrite(_ id: Int) {
        pending.removeValue(forKey: id)?.completion(.failure(MLDaemonError.writeFailed))
    }

    private func cancelRequest(_ id: Int) {
        // Detach the caller without restarting the daemon or discarding its loaded models.
        pending.removeValue(forKey: id)?.completion(.failure(CancellationError()))
    }

    private func handle(line: String) {
        guard let data = line.data(using: .utf8) else {
            logger.error("Failed to decode daemon line")
            return
        }
        guard
            let json = try? JSONSerialization.jsonObject(with: data, options: []) as? [String: Any],
            let id = json["id"] as? Int
        else {
            logger.error("Malformed JSON-RPC response")
            return
        }

        guard let pendingRequest = pending.removeValue(forKey: id) else {
            logger.error("No pending request for id \(id, privacy: .public)")
            return
        }

        if let error = json["error"] as? [String: Any],
           let message = error["message"] as? String {
            pendingRequest.completion(.failure(MLDaemonError.remoteError(message)))
            return
        }

        guard let result = json["result"] else {
            pendingRequest.completion(.failure(MLDaemonError.invalidResponse("Missing result")))
            return
        }

        do {
            let resultData = try JSONSerialization.data(withJSONObject: result, options: [.fragmentsAllowed])
            pendingRequest.completion(.success(resultData))
        } catch {
            pendingRequest.completion(.failure(MLDaemonError.invalidResponse(error.localizedDescription)))
        }
    }

    // MARK: - Process lifecycle

    private func ensureDaemonRunning() throws {
        guard !isShuttingDown else { throw MLDaemonError.daemonUnavailable("shutting down") }
        if let process, process.isRunning { return }
        guard restartAttempts < maxRestartAttempts else { throw MLDaemonError.restartLimitReached }
        let isRestart = process != nil
        if isRestart {
            closePipes()
            completeAllPending(with: MLDaemonError.daemonUnavailable("process exited"))
            process = nil
            processGeneration = nil
        }
        try startProcess(isRestart: isRestart)
    }

    private func startProcess(isRestart: Bool) throws {
        let python = try resolvedPython()
        let script = try resolvedScript()

        if isRestart { restartAttempts += 1 } else { restartAttempts = 0 }
        if restartAttempts > maxRestartAttempts { throw MLDaemonError.restartLimitReached }

        let proc = Process()
        proc.executableURL = python
        proc.arguments = [script.path]
        let defaults = UserDefaults.standard
        let origins = defaults.stringArray(forKey: AppDefaults.Keys.localhostSTTOrigins)
            ?? ["http://127.0.0.1:*", "http://localhost:*"]
        let originsData = try JSONSerialization.data(withJSONObject: origins)
        proc.environment = ProcessInfo.processInfo.environment.merging([
            "PYTHONUNBUFFERED": "1",
            "SCRIBE_STT_ENABLED": AppEnvironment.isRunningTests ? "0" :
                ((defaults.object(forKey: AppDefaults.Keys.localhostSTTEnabled) as? Bool ?? true) ? "1" : "0"),
            "SCRIBE_STT_PORT": String(defaults.object(forKey: AppDefaults.Keys.localhostSTTPort) as? Int ?? 8111),
            "SCRIBE_STT_ORIGINS": String(decoding: originsData, as: UTF8.self),
            "SCRIBE_STT_VERSION": VersionInfo.version,
            "SCRIBE_RECORDING_ACTIVE": recordingActive ? "1" : "0"
        ]) { _, new in new }

        let stdin = Pipe()
        let stdout = Pipe()
        let stderr = Pipe()

        // A closed child pipe must throw a write error, not deliver SIGPIPE to the app.
        guard fcntl(stdin.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) != -1 else {
            throw MLDaemonError.writeFailed
        }
        proc.standardInput = stdin
        proc.standardOutput = stdout
        proc.standardError = stderr

        let generation = UUID()
        proc.terminationHandler = { [weak self] process in
            Task { await self?.processTerminated(exitCode: process.terminationStatus, generation: generation) }
        }

        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            let message = String(decoding: data, as: UTF8.self)
            self?.logger.error("ml_daemon stderr: \(message, privacy: .public)")
        }

        do {
            try proc.run()
        } catch {
            throw MLDaemonError.daemonUnavailable("Failed to start process: \(error.localizedDescription)")
        }

        process = proc
        processGeneration = generation
        stdinPipe = stdin
        stdoutPipe = stdout
        stderrPipe = stderr
        isShuttingDown = false
        startStdoutReader(pipe: stdout)
    }

    private func startStdoutReader(pipe: Pipe) {
        stdoutReaderTask?.cancel()
        let handle = pipe.fileHandleForReading
        stdoutReaderTask = Task { [weak self] in
            do {
                for try await line in handle.bytes.lines {
                    guard !line.isEmpty else { continue }
                    await self?.handle(line: line)
                }
            } catch is CancellationError {
                return
            } catch {
                await self?.handleStdoutReaderError(error)
            }
        }
    }

    private func processTerminated(exitCode: Int32, generation: UUID) async {
        // A delayed termination callback from an old process must not close a replacement's pipes.
        guard generation == processGeneration else { return }
        processGeneration = nil
        logger.error("ml_daemon exited with code \(exitCode)")
        closePipes()

        if isShuttingDown {
            process = nil
            return
        }

        completeAllPending(with: MLDaemonError.daemonUnavailable("exited (\(exitCode))"))
        process = nil

        guard restartAttempts < maxRestartAttempts else { return }

        do {
            try startProcess(isRestart: true)
        } catch {
            logger.error("Failed to restart ml_daemon: \(error.localizedDescription)")
            completeAllPending(with: error)
        }
    }

    private func closePipes() {
        stdoutReaderTask?.cancel()
        stdoutReaderTask = nil
        stdinPipe?.fileHandleForWriting.closeFile()
        stdoutPipe?.fileHandleForReading.readabilityHandler = nil
        stdoutPipe?.fileHandleForReading.closeFile()
        stderrPipe?.fileHandleForReading.readabilityHandler = nil
        stderrPipe?.fileHandleForReading.closeFile()
        stdinPipe = nil
        stdoutPipe = nil
        stderrPipe = nil
    }

    func shutdown() async {
        isShuttingDown = true
        // SIGTERM drains HTTP independently of the model worker. Closing stdin
        // also handles a parent exiting before this actor gets another turn.
        process?.terminate()
        closePipes()
        process = nil
        processGeneration = nil
        completeAllPending(with: MLDaemonError.daemonUnavailable("shutdown"))
    }

    private func completeAllPending(with error: Error) {
        for (_, pendingRequest) in pending {
            pendingRequest.completion(.failure(error))
        }
        pending.removeAll()
    }

    private func handleStdoutReaderError(_ error: Error) {
        guard !isShuttingDown else { return }
        logger.error("ml_daemon stdout reader failed: \(error.localizedDescription)")
    }

    // MARK: - Helpers

    private func resolvedPython() throws -> URL {
        if let pythonExecutable { return pythonExecutable }
        let url = try UvBootstrap.ensureVenv(userPython: nil)
        pythonExecutable = url
        return url
    }

    private func resolvedScript() throws -> URL {
        if let scriptLocation { return scriptLocation }
        if let bundled = ResourceLocator.pythonScriptURL(named: "ml_daemon") {
            scriptLocation = bundled
            return bundled
        }

        throw MLDaemonError.scriptNotFound
    }
}

#if DEBUG
internal extension MLDaemonManager {
    var pendingRequestCountForTesting: Int { pending.count }
    var processIdentifierForTesting: Int32? { process?.processIdentifier }

    func setTestResponder(_ responder: ((String, [String: Any]) throws -> Any)?) {
        testResponder = responder
    }

    /// Allows tests to bypass the default Python resolution and bundled script lookup.
    func setTestOverrides(python: URL?, script: URL?) async {
        pythonExecutable = python
        scriptLocation = script
    }

    /// Resets state for isolation between tests, ensuring processes are terminated and overrides cleared.
    func resetForTesting() async {
        await shutdown()
        pythonExecutable = nil
        scriptLocation = nil
        restartAttempts = 0
        isShuttingDown = false
        testResponder = nil
        recordingActive = false
    }
}
#endif
