import AppKit

internal enum TranscriptPresentation {
    static let width: CGFloat = 640
    static let compactHeight: CGFloat = 300
    static let liveHeight: CGFloat = 300
    static let defaultAvailableSize = CGSize(width: 1440, height: 1000)
    static let sideWidth: CGFloat = 172
    static let chromeHeight: CGFloat = 94

    struct Layout {
        let size: CGSize
        let text: String
        let isTruncated: Bool
        var textWidth: CGFloat { size.width - sideWidth - 60 }
        var bodyHeight: CGFloat { size.height - chromeHeight }
    }

    static func duration(for text: String) -> TimeInterval {
        let words = text.split(whereSeparator: { $0.isWhitespace }).count
        return min(6, max(0.85, 0.65 + Double(words) * 0.045))
    }

    static func size(finalText: String?, live: Bool, liveText: String = "",
                     available: CGSize = defaultAvailableSize) -> CGSize {
        layout(text: finalText ?? liveText, live: finalText == nil && live, available: available).size
    }

    static func layout(text: String, live: Bool, available: CGSize = defaultAvailableSize) -> Layout {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let maxWidth = max(320, min(1000, available.width - 48))
        let maxHeight = max(220, min(840, available.height - 80))
        let heightLimit = min(560, maxHeight)
        var panelWidth = min(width, maxWidth)
        func measured(_ value: String, width: CGFloat) -> CGFloat {
            textHeight(value, width: width - sideWidth - 60)
        }
        // A character-count lower bound on the wrapped line count skips full-text
        // measurement once the transcript cannot possibly fit the widest panel.
        // No SF Pro glyph at 15 pt renders narrower than three points, so the
        // estimate overestimates characters per line and underestimates lines:
        // it only rejects text that is certain to overflow, and the exact path
        // below stays authoritative for anything shorter.
        func cannotFit(_ width: CGFloat) -> Bool {
            let textWidth = width - sideWidth - 60
            let maximumCharsPerLine = max(1, Int(textWidth / 3))
            let minimumLines = (text.count + maximumCharsPerLine - 1) / maximumCharsPerLine
            return CGFloat(minimumLines) * excerptLineHeight > heightLimit - chromeHeight - 16
        }
        let certainToTruncate = cannotFit(maxWidth)
        if certainToTruncate {
            panelWidth = maxWidth
        } else {
            // Widen longer dictations before asking the user to scan a tall narrow column.
            while measured(text, width: panelWidth) + chromeHeight + 16 > heightLimit, panelWidth < maxWidth {
                panelWidth = min(maxWidth, panelWidth + 120)
            }
        }
        let panelHeight = certainToTruncate ? maxHeight
            : min(maxHeight, max(compactHeight, ceil(measured(text, width: panelWidth)) + chromeHeight + 16))
        let size = CGSize(width: panelWidth, height: panelHeight)
        if !certainToTruncate {
            guard measured(text, width: panelWidth) + 8 > panelHeight - chromeHeight else {
                return Layout(size: size, text: text, isTruncated: false)
            }
        }
        // Beyond the screen-sized limit, show a clearly labelled excerpt. Never
        // hide a scrollbar or silently clip a line; clipboard/history keep all text.
        let characters = Array(text)
        let availableHeight = panelHeight - chromeHeight - 34
        // The excerpt can never exceed the characters that fit the available
        // lines, so the search starts from that bound instead of the full length.
        let maximumDisplayedCharacters = max(1, Int(availableHeight / excerptLineHeight)
            * max(1, Int((panelWidth - sideWidth - 60) / 3)))
        var low = 0, high = min(characters.count, maximumDisplayedCharacters)
        while low < high {
            let middle = (low + high + 1) / 2
            let candidate = live ? String(characters.suffix(middle)) : String(characters.prefix(middle))
            if measured(candidate + "…", width: panelWidth) <= availableHeight { low = middle }
            else { high = middle - 1 }
        }
        var excerpt = live ? String(characters.suffix(low)) : String(characters.prefix(low))
        // Prefer a complete word at the exposed boundary when the language uses spaces.
        if live, let boundary = excerpt.firstIndex(where: { $0.isWhitespace }) {
            excerpt = String(excerpt[boundary...]).trimmingCharacters(in: .whitespacesAndNewlines)
        } else if !live, let boundary = excerpt.lastIndex(where: { $0.isWhitespace }) {
            excerpt = String(excerpt[..<boundary])
        }
        return Layout(size: size, text: live ? "…" + excerpt : excerpt + "…", isTruncated: true)
    }

    /// Height of one wrapped 15 pt line with the transcript line spacing; the
    /// conservative layout bound above and the excerpt search share it.
    static let excerptLineHeight: CGFloat = textHeight("X", width: 40)

    static func textHeight(_ text: String, width: CGFloat) -> CGFloat {
        guard !text.isEmpty else { return 0 }
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = 4
        return (text as NSString).boundingRect(
            with: CGSize(width: max(40, width), height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            attributes: [.font: NSFont.systemFont(ofSize: 15), .paragraphStyle: paragraph]
        ).height
    }
}
