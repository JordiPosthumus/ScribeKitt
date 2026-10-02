import Foundation
import Combine

@MainActor
internal final class LocalSetupManager: ObservableObject {
    static let shared = LocalSetupManager()

    enum Stage: Int, CaseIterable {
        case runtime, download, verification
        var title: String {
            switch self {
            case .runtime: return "Prepare the local runtime"
            case .download: return "Download the speech model"
            case .verification: return "Check offline dictation"
            }
        }
    }

    enum State: Equatable {
        case waiting, preparing(Stage), ready, failed(String), unsupported
    }

    @Published private(set) var state: State
    private let runtime: () async throws -> Void
    private let download: () async throws -> Void
    private let verify: () async throws -> Void

    var isReady: Bool { state == .ready }
    var isPreparing: Bool {
        if case .preparing = state { return true }
        return false
    }

    nonisolated static var supportsThisMac: Bool {
        #if arch(arm64)
        true
        #else
        false
        #endif
    }

    nonisolated static func existingInstallationIsReady() -> Bool {
        guard ParakeetService().isModelCached(), let project = try? UvBootstrap.projectDir() else { return false }
        return FileManager.default.isExecutableFile(atPath: project.appendingPathComponent(".venv/bin/python3").path)
            || FileManager.default.isExecutableFile(atPath: project.appendingPathComponent(".venv/bin/python").path)
    }

    init(supported: Bool = supportsThisMac,
         existingInstallation: () -> Bool = existingInstallationIsReady,
         runtime: @escaping () async throws -> Void = { try await MLDaemonManager.shared.prepareRuntime() },
         download: @escaping () async throws -> Void = { try await MLDaemonManager.shared.prepareModel() },
         verify: @escaping () async throws -> Void = {
             try await MLDaemonManager.shared.verifySetup()
         }) {
        self.runtime = runtime
        self.download = download
        self.verify = verify
        state = supported ? (existingInstallation() ? .ready : .waiting) : .unsupported
    }

    func prepare() async {
        guard !isReady, !isPreparing, state != .unsupported else { return }
        do {
            state = .preparing(.runtime)
            try await runtime()
            state = .preparing(.download)
            try await download()
            state = .preparing(.verification)
            try await verify()
            state = .ready
            if !AppEnvironment.isRunningTests { await LocalSTTSettings.shared.apply() }
        } catch {
            state = .failed(error.localizedDescription)
        }
    }
}
