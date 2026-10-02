import Foundation

/// Centralized defaults so "fresh install" behavior is deterministic and consistent across the app.
///
/// Notes:
/// - We use `register(defaults:)` (registration domain) rather than eagerly writing values, so we don't
///   accidentally clobber user preferences or treat a first-run as "already configured".
/// - AppStorage initial values across the app should match these constants.
internal enum AppDefaults {
    internal enum Keys {
        static let transcriptionProvider = "transcriptionProvider"
        static let selectedParakeetModel = "selectedParakeetModel"


        static let startAtLogin = "startAtLogin"
        static let playCompletionSound = "playCompletionSound"
        static let transcriptionHistoryEnabled = "transcriptionHistoryEnabled"
        static let transcriptionRetentionPeriod = "transcriptionRetentionPeriod"
        static let enableSmartPaste = "enableSmartPaste"
        static let immediateRecording = "immediateRecording"
        static let transcriptionStreaming = "transcriptionStreaming"
        static let addTrailingSpace = "addTrailingSpace"
        static let localhostSTTEnabled = "localhostSTTEnabled"
        static let localhostSTTPort = "localhostSTTPort"
        static let localhostSTTOrigins = "localhostSTTOrigins"
        static let globalHotkey = "globalHotkey"
        static let autoBoostMicrophoneVolume = "autoBoostMicrophoneVolume"

        static let pressAndHoldEnabled = "pressAndHoldEnabled"
        static let pressAndHoldKeyIdentifier = "pressAndHoldKeyIdentifier"
        static let pressAndHoldMode = "pressAndHoldMode"

    }

    // Chosen defaults.
    internal static let defaultTranscriptionProvider: TranscriptionProvider = .parakeet
    internal static let defaultParakeetModel: ParakeetModel = .v2English
    internal static let defaultGlobalHotkey = "⌥⌃Q"

    internal static func streamingEnabled(in defaults: UserDefaults = .standard) -> Bool {
        defaults.object(forKey: Keys.transcriptionStreaming) as? Bool ?? true
    }

    internal static func register(using defaults: UserDefaults = .standard) {
        defaults.register(defaults: [
            Keys.transcriptionProvider: defaultTranscriptionProvider.rawValue,
            Keys.selectedParakeetModel: defaultParakeetModel.rawValue,


            Keys.startAtLogin: false,
            Keys.playCompletionSound: true,
            Keys.transcriptionHistoryEnabled: true,
            Keys.transcriptionRetentionPeriod: RetentionPeriod.forever.rawValue,
            Keys.autoBoostMicrophoneVolume: true,
            Keys.enableSmartPaste: false,
            Keys.immediateRecording: false,
            Keys.transcriptionStreaming: true,
            Keys.addTrailingSpace: true,
            Keys.localhostSTTEnabled: true,
            Keys.localhostSTTPort: 8111,
            Keys.localhostSTTOrigins: ["http://127.0.0.1:*", "http://localhost:*"],
            Keys.globalHotkey: defaultGlobalHotkey,

            Keys.pressAndHoldEnabled: PressAndHoldConfiguration.defaults.enabled,
            Keys.pressAndHoldKeyIdentifier: PressAndHoldConfiguration.defaults.key.rawValue,
            Keys.pressAndHoldMode: PressAndHoldConfiguration.defaults.mode.rawValue

        ])
    }
}
