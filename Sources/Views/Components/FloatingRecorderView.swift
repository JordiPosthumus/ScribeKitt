import SwiftUI

/// Voice-driven feedback, provisional live words, and a self-dismissing clipboard confirmation.
internal struct FloatingRecorderView: View {
    let status: AppStatus
    let audioLevel: Float
    let recordingStartedAt: Date?
    var waveformSamples: [Float] = []
    var stableText = ""
    var draftText = ""
    var streaming = false
    var preparingPreview = false
    var previewProblem: String?
    var finalText: String?
    var availableSize = TranscriptPresentation.defaultAvailableSize
    let onPrimaryAction: () -> Void
    let onDismiss: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var completionGlow = false
    @State private var cachedLayout = TranscriptPresentation.layout(text: "", live: true)
    @State private var transcriptWordCount = 0
    // The 10 Hz meter re-evaluates this body; caching the concatenated live
    // transcript keeps those evaluations free of full-transcript allocations.
    // It changes only when the preview itself changes (about 1 Hz).
    @State private var liveTranscript = ""
    private let cyan = Color(red: 0.30, green: 0.91, blue: 0.97)
    private let lilac = Color(red: 0.65, green: 0.53, blue: 1)
    private var recording: Bool { if case .recording = status { return true }; return false }
    private var processing: Bool { if case .processing = status { return true }; return false }
    private var transcript: String { finalText ?? liveTranscript }
    private var layout: TranscriptPresentation.Layout { cachedLayout }
    private var size: CGSize { layout.size }
    private var hasLiveWords: Bool { !liveTranscript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    private var levels: [Double] {
        if reduceMotion { return Array(repeating: AudioLevelDisplay.clamped(audioLevel), count: 48) }
        return waveformSamples.isEmpty ? Array(repeating: 0, count: 48) : waveformSamples.map(AudioLevelDisplay.clamped)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header.padding(.horizontal, 19).padding(.top, 15)
            HStack(alignment: .top, spacing: 20) {
                KITTVoicePanel(levels: levels, active: recording, processing: processing,
                    complete: finalText != nil, streaming: streaming,
                    recordingStartedAt: recordingStartedAt,
                    wordCount: transcriptWordCount)
                    .frame(width: TranscriptPresentation.sideWidth, height: 200)
                transcriptBody
                    .frame(width: layout.textWidth, alignment: .topLeading)
            }
            .frame(height: layout.bodyHeight, alignment: .top)
            .padding(.horizontal, 20).padding(.top, 16)
            Capsule()
                .fill(LinearGradient(colors: [cyan.opacity(0.12), cyan, lilac.opacity(0.7)], startPoint: .leading, endPoint: .trailing))
                .frame(height: 2)
                .scaleEffect(x: completionGlow ? 1 : 0, y: 1, anchor: .leading)
                .opacity(finalText == nil ? 0 : 1)
                .padding(.horizontal, 20).padding(.top, 12).padding(.bottom, 16)
        }
        .frame(width: size.width, height: size.height, alignment: .top)
        .background {
            ZStack {
                RoundedRectangle(cornerRadius: 25, style: .continuous)
                    .fill(Color(red: 0.035, green: 0.050, blue: 0.083))
                RoundedRectangle(cornerRadius: 25, style: .continuous)
                    .fill(LinearGradient(colors: [lilac.opacity(0.12), .clear, cyan.opacity(completionGlow ? 0.19 : 0.035)], startPoint: .topLeading, endPoint: .bottomTrailing))
            }
        }
        .overlay {
            RoundedRectangle(cornerRadius: 25, style: .continuous)
                .stroke(LinearGradient(colors: [cyan.opacity(completionGlow ? 0.8 : 0.32), .white.opacity(0.06), lilac.opacity(0.38)], startPoint: .topLeading, endPoint: .bottomTrailing), lineWidth: 1)
        }
        .clipShape(RoundedRectangle(cornerRadius: 25, style: .continuous))
        .environment(\.colorScheme, .dark)
        .onChange(of: finalText) { _, text in refreshLayout(); animateCompletion(text != nil) }
        .onChange(of: transcript) { _, _ in refreshLayout() }
        .onChange(of: availableSize) { _, _ in refreshLayout() }
        .onChange(of: stableText) { _, _ in liveTranscript = stableText + draftText }
        .onChange(of: draftText) { _, _ in liveTranscript = stableText + draftText }
        .onAppear {
            liveTranscript = stableText + draftText
            refreshLayout(); if finalText != nil { animateCompletion(true) }
        }
    }

    private func refreshLayout() {
        // Text measurement is tied to transcript updates, never the 10 Hz meter.
        cachedLayout = TranscriptPresentation.layout(text: transcript, live: finalText == nil, available: availableSize)
        transcriptWordCount = transcript.split(whereSeparator: { $0.isWhitespace }).count
    }

    private func animateCompletion(_ complete: Bool) {
        completionGlow = false
        guard complete else { return }
        if reduceMotion { completionGlow = true }
        else { withAnimation(.easeOut(duration: 0.4)) { completionGlow = true } }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Button(action: onPrimaryAction) {
                ZStack {
                    Circle().fill(cyan.opacity(finalText == nil ? 0.09 : 0.17))
                    Circle().stroke(cyan.opacity(0.25), lineWidth: 1)
                    Image(systemName: finalText != nil ? "checkmark" : recording ? "stop.fill" : "waveform")
                        .font(.system(size: finalText != nil ? 13 : 11, weight: .bold))
                        .foregroundStyle(cyan)
                        .scaleEffect(finalText != nil && !completionGlow && !reduceMotion ? 0.6 : 1)
                }
                .frame(width: 32, height: 32)
            }
            .buttonStyle(.plain)
            .disabled(processing || finalText != nil)
            .help(recording ? "Stop recording" : "Start recording")
            .accessibilityLabel(finalText != nil ? "Copied" : recording ? "Stop recording" : "Start recording")
            VStack(alignment: .leading, spacing: 3) {
                Text(finalText != nil ? "Copied" : recording ? "Listening" : processing ? "Finalizing" : "ScribeKitt")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.94))
                Text(finalText != nil ? "Ready for ⌘V" : processing ? "Checking the complete recording" : streaming ? "LOCAL · LIVE DICTATION" : "LOCAL DICTATION")
                    .font(.system(size: finalText != nil || processing ? 10 : 8, weight: .medium, design: .rounded))
                    .tracking(finalText != nil || processing ? 0 : 1.2)
                    .foregroundStyle(.white.opacity(0.43))
            }
            Spacer(minLength: 8)
            if processing { ProgressView().controlSize(.small).tint(cyan) }
            Button(action: onDismiss) {
                Image(systemName: "xmark").font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.35))
                    .frame(width: 20, height: 24).contentShape(Rectangle())
            }
            .buttonStyle(.plain).accessibilityLabel("Dismiss recorder")
        }
    }

    private var transcriptBody: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !layout.text.isEmpty {
                transcriptText
                    .font(.system(size: 15)).lineSpacing(4)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                Text(previewProblem ?? (preparingPreview ? "Preparing live words…" : processing ? "Your final text is on its way…" : streaming ? "Your words will appear here as you speak." : "Speak naturally. Your complete transcript will appear when you stop."))
                    .font(.system(size: 15)).lineSpacing(4).foregroundStyle(.white.opacity(0.40))
                    .fixedSize(horizontal: false, vertical: true)
            }
            if layout.isTruncated {
                Text(finalText == nil ? "Showing the latest words · the final pass includes everything" : "Display limit reached · the complete text is on your clipboard")
                    .font(.system(size: 11)).foregroundStyle(.white.opacity(0.45))
            } else if previewProblem != nil, hasLiveWords, finalText == nil {
                Text("Live preview paused · final transcription continues")
                    .font(.system(size: 11)).foregroundStyle(.white.opacity(0.45))
            }
        }
    }

    private var transcriptText: Text {
        guard finalText == nil else { return Text(layout.text).foregroundColor(.white.opacity(0.94)) }
        guard !layout.isTruncated else { return Text(layout.text).foregroundColor(cyan.opacity(0.88)) }
        let earlierCount = stableText.drop(while: { $0.isWhitespace }).count
        let boundary = min(earlierCount, layout.text.count)
        return Text(String(layout.text.prefix(boundary))).foregroundColor(.white.opacity(0.92)) +
            Text(String(layout.text.dropFirst(boundary))).foregroundColor(cyan.opacity(0.88))
    }
}

internal enum AudioLevelDisplay {
    static func clamped(_ value: Float) -> Double {
        guard value.isFinite else { return 0 }
        return Double(min(1, max(0, value)))
    }
    static func isLit(index: Int, count: Int, level: Double) -> Bool {
        level > Double(index) / Double(count)
    }
}

/// Three centered LED columns inspired by KITT's dashboard voice modulator.
/// This view updates only with the existing microphone meter; it has no clock.
internal struct VoiceRibbon: View {
    let levels: [Double]
    let active: Bool

    private func recentLevel(_ count: Int) -> Double {
        let samples = levels.suffix(count)
        guard active, !samples.isEmpty else { return 0 }
        return min(1, max(0, samples.reduce(0, +) / Double(samples.count)))
    }

    var body: some View {
        Canvas { context, size in
            let rows = 20
            let columnWidth = 14.0
            let gap = 6.0
            let width = columnWidth * 3 + gap * 2
            let origin = (size.width - width) / 2
            let red = Color(red: 1, green: 0.065, blue: 0.09)
            let voice = recentLevel(1)
            // Center follows the present syllable; the flanks use a short envelope
            // of real readings. These are visual envelopes, not frequency bands.
            let envelopes = [recentLevel(2) * 0.76, voice, recentLevel(3) * 0.76]
            let lamp = Gradient(colors: [Color(red: 1, green: 0.31, blue: 0.22), red,
                                         Color(red: 0.75, green: 0.015, blue: 0.055)])

            // Fine, stationary rails frame the voice box without filling the width.
            for side in [0, 1] {
                let x = side == 0 ? origin - 10 : origin + width + 9
                context.fill(Path(CGRect(x: x, y: 1, width: 1, height: size.height - 2)),
                             with: .color(red.opacity(0.10)))
            }
            for column in 0..<3 {
                let columnHeight = size.height * (column == 1 ? 1 : 0.88)
                let top = (size.height - columnHeight) / 2
                let pitch = (columnHeight - 2) / Double(rows)
                let strength = pow(envelopes[column], 1.1)
                let litPairs = Int((strength * Double(rows / 2)).rounded())
                let x = origin + Double(column) * (columnWidth + gap)
                if litPairs > 0 {
                    let center = CGPoint(x: x + columnWidth / 2, y: size.height / 2)
                    context.fill(Path(ellipseIn: CGRect(x: x - 9, y: 0, width: columnWidth + 18, height: size.height)),
                        with: .radialGradient(Gradient(colors: [red.opacity(strength * 0.15), .clear]),
                            center: center, startRadius: 0, endRadius: size.height / 2))
                }
                for row in 0..<rows {
                    let distance = abs(Double(row) - Double(rows - 1) / 2)
                    let lit = distance < Double(litPairs)
                    let rect = CGRect(x: x, y: top + 1 + Double(row) * pitch,
                                      width: columnWidth, height: max(1, pitch - 1.4))
                    var light = context
                    light.opacity = lit ? 0.76 + strength * 0.24 : 0.045
                    light.fill(Path(roundedRect: rect, cornerRadius: 0.35), with: .linearGradient(lamp,
                        startPoint: CGPoint(x: rect.midX, y: rect.minY),
                        endPoint: CGPoint(x: rect.midX, y: rect.maxY)))
                }
            }
        }
        .accessibilityElement(children: .ignore).accessibilityLabel("Microphone voice level")
    }
}

/// Decorative status lamps occupy the classic voice-box positions; none are controls.
private struct KITTVoicePanel: View {
    let levels: [Double]
    let active: Bool
    let processing: Bool
    let complete: Bool
    let streaming: Bool
    let recordingStartedAt: Date?
    let wordCount: Int
    private let amber = Color(red: 1, green: 0.66, blue: 0.27)
    private let red = Color(red: 1, green: 0.13, blue: 0.15)

    var body: some View {
        VStack(spacing: 12) {
            HStack(alignment: .center, spacing: 6) {
                VStack(spacing: 12) {
                    lamp("LOCAL", color: amber, lit: true)
                    lamp("MIC", color: amber, lit: active)
                    lamp("REC", color: red, lit: active)
                    lamp("LIVE", color: red, lit: streaming && active)
                }
                VoiceRibbon(levels: levels, active: active).frame(width: 54, height: 148)
                VStack(spacing: 12) {
                    Group {
                        if active, let recordingStartedAt {
                            Text(recordingStartedAt, style: .timer).monospacedDigit()
                        } else { Text("READY") }
                    }
                    .font(.system(size: 8, weight: .bold, design: .rounded))
                    .foregroundStyle(Color.black.opacity(0.9))
                    .frame(width: 39, height: 19)
                    .background(amber.opacity(0.82), in: Capsule())
                    .accessibilityLabel(active ? "Elapsed recording time" : "Ready")
                    lamp("\(wordCount) W", color: amber, lit: wordCount > 0)
                    lamp("FINAL", color: red, lit: processing)
                    lamp("COPIED", color: red, lit: complete)
                }
            }
            Text(complete ? "COPIED" : processing ? "FINAL PASS" : active ? "LISTENING" : "STANDBY")
                .font(.system(size: 9, weight: .heavy, design: .rounded)).tracking(1.2)
                .foregroundStyle(red.opacity(0.9))
                .frame(width: 114, height: 20)
                .background(red.opacity(0.10), in: RoundedRectangle(cornerRadius: 3))
                .overlay(RoundedRectangle(cornerRadius: 3).stroke(red.opacity(0.22), lineWidth: 1))
        }
        .padding(.vertical, 10).padding(.horizontal, 8)
        .background(Color.black.opacity(0.45), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(.white.opacity(0.06), lineWidth: 1))
    }

    private func lamp(_ text: String, color: Color, lit: Bool) -> some View {
        Text(text).font(.system(size: 8, weight: .heavy, design: .rounded))
            .foregroundStyle(lit ? Color.black.opacity(0.9) : color.opacity(0.40))
            .frame(width: 39, height: 19)
            .background(color.opacity(lit ? 0.82 : 0.07), in: Capsule())
            .overlay(Capsule().stroke(color.opacity(lit ? 0.20 : 0.08), lineWidth: 0.5))
    }
}
