import SwiftUI

internal struct DashboardPreferencesView: View {
    @AppStorage("immediateRecording") private var immediateRecording = false
    @AppStorage(AppDefaults.Keys.transcriptionStreaming) private var transcriptionStreaming = true
    @AppStorage(AppDefaults.Keys.addTrailingSpace) private var addTrailingSpace = true
    @AppStorage(AppDefaults.Keys.localhostSTTEnabled) private var localhostSTTEnabled = true
    @AppStorage(AppDefaults.Keys.localhostSTTPort) private var localhostSTTPort = 8111
    @StateObject private var localhostSTT = LocalSTTSettings.shared
    @State private var portText = "8111"
    @State private var portError: String?
    @AppStorage("autoBoostMicrophoneVolume") private var autoBoostMicrophoneVolume = true
    @AppStorage("playCompletionSound") private var playCompletionSound = true
    @AppStorage("transcriptionHistoryEnabled") private var transcriptionHistoryEnabled = true
    @AppStorage("transcriptionRetentionPeriod") private var transcriptionRetentionPeriodRaw = RetentionPeriod.forever.rawValue



    private var retentionBinding: Binding<RetentionPeriod> {
        Binding(
            get: { RetentionPeriod(rawValue: transcriptionRetentionPeriodRaw) ?? .forever },
            set: { transcriptionRetentionPeriodRaw = $0.rawValue }
        )
    }

    var body: some View {
        Form {
            Section("General") {
                LoginItemControlView()

                Toggle(isOn: $immediateRecording) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Express Mode")
                        Text("Hotkey immediately starts and stops recording.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                Toggle(isOn: $autoBoostMicrophoneVolume) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Auto-Boost Microphone")
                        Text("Temporarily maximize mic input while recording.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                LabeledContent("Paste", value: "Copied automatically; paste with ⌘V")

                Toggle(isOn: $addTrailingSpace) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Add Trailing Space")
                        Text("Add a space after sentence-ending punctuation so your next dictation pastes separately.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                Toggle(isOn: $transcriptionStreaming) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Transcription Streaming")
                        Text("Show live words while you speak. Changes apply to your next recording; the final pass always checks the complete audio.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                Toggle(isOn: $playCompletionSound) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Completion Sound")
                        Text("Play a chime when transcription finishes.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

            }

            Section {
                Toggle(isOn: $transcriptionHistoryEnabled) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Save Transcription History")
                        Text("Store transcripts locally so you can search and review them later.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                if transcriptionHistoryEnabled {
                    Picker("Retention Period", selection: retentionBinding) {
                        ForEach(RetentionPeriod.allCases, id: \.rawValue) { period in
                            Text(period.displayName).tag(period)
                        }
                    }
                    .pickerStyle(.menu)
                }
            } header: {
                Text("History")
            } footer: {
                Text("View saved transcripts in the Transcripts tab.")
            }

            Section("Localhost transcription API") {
                Toggle("Expose localhost transcription API", isOn: $localhostSTTEnabled)
                    .onChange(of: localhostSTTEnabled) { _, _ in
                        Task { await localhostSTT.apply() }
                    }
                HStack {
                    TextField("Port (0 chooses an available port)", text: $portText)
                    Button("Apply") {
                        guard let port = Int(portText), (0...65535).contains(port) else {
                            portError = "Enter a port from 0 to 65535"
                            return
                        }
                        portError = nil
                        localhostSTTPort = port
                        Task { await localhostSTT.apply() }
                    }
                }
                if let portError { Text(portError).foregroundStyle(.red) }
                Text(localhostSTT.status).textSelection(.enabled)
                HStack {
                    Text(localhostSTT.tokenPath)
                        .font(.caption)
                        .textSelection(.enabled)
                    Button("Copy token path") { localhostSTT.copyTokenPath() }
                }
                Text("Shares the resident speech model with local apps. Dictation takes priority. The API returns 503 until the model is loaded.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .task {
                portText = String(localhostSTTPort)
                await localhostSTT.apply()
            }

            Section("About") {
                Text("Built on the original AudioWhisper project.")
                    .foregroundStyle(.secondary)
                Link("AudioWhisper by mazdak and contributors", destination: URL(string: "https://github.com/mazdak/AudioWhisper")!)
                Text("Original MIT license and attribution retained.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                LabeledContent("Version") {
                    Text(VersionInfo.version)
                        .font(.system(.body, design: .monospaced))
                        .foregroundStyle(.secondary)
                }

            }
        }
        .formStyle(.grouped)
    }


}

#Preview {
    DashboardPreferencesView()
        .frame(width: 900, height: 700)
}
