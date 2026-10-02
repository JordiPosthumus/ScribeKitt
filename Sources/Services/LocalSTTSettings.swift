import AppKit
import Combine
import Foundation
import os.log

@MainActor
internal final class LocalSTTSettings: ObservableObject {
    static let shared = LocalSTTSettings()
    @Published private(set) var status = "Localhost API has not started"
    @Published private(set) var tokenPath = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/ScribeKit/stt_server.token").path
    private var revision = 0

    func apply() async {
        guard !AppEnvironment.isRunningTests else { return }
        guard LocalSetupManager.shared.isReady else {
            status = "Finish Prepare ScribeKitt to start the localhost API"
            return
        }
        revision += 1
        let current = revision
        let defaults = UserDefaults.standard
        let port = defaults.object(forKey: AppDefaults.Keys.localhostSTTPort) as? Int ?? 8111
        guard (0...65535).contains(port) else {
            status = "Port must be between 0 and 65535"
            return
        }
        do {
            let result = try await MLDaemonManager.shared.configureSTT(
                enabled: defaults.object(forKey: AppDefaults.Keys.localhostSTTEnabled) as? Bool ?? true,
                port: port,
                origins: defaults.stringArray(forKey: AppDefaults.Keys.localhostSTTOrigins)
                    ?? ["http://127.0.0.1:*", "http://localhost:*"])
            guard current == revision else { return }
            tokenPath = result.token_path
            if let error = result.error {
                status = error
                Logger.app.error("\(error, privacy: .public)")
            } else if result.running, let actualPort = result.port {
                status = "Listening on 127.0.0.1:\(actualPort)"
            } else {
                status = "Localhost API is off"
            }
        } catch {
            guard current == revision else { return }
            status = "Localhost API: \(error.localizedDescription)"
            Logger.app.error("\(self.status, privacy: .public)")
        }
    }

    func copyTokenPath() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(tokenPath, forType: .string)
    }
}
