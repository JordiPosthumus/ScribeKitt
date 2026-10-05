import Foundation

struct VersionInfo {
    static let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "210.19"
    static let gitHash = Bundle.main.object(forInfoDictionaryKey: "AudioWhisperGitRevision") as? String ?? "dev-build"
    static let buildDate = Bundle.main.object(forInfoDictionaryKey: "AudioWhisperBuildDate") as? String ?? ""
    
    static var displayVersion: String {
        if gitHash != "dev-build" && gitHash != "unknown" && !gitHash.isEmpty {
            let shortHash = String(gitHash.prefix(7))
            return "\(version) (\(shortHash))"
        }
        return version
    }
    
    static var fullVersionInfo: String {
        var info = "\(AppBrand.name) \(version)"
        if gitHash != "dev-build" && gitHash != "unknown" && !gitHash.isEmpty {
            let shortHash = String(gitHash.prefix(7))
            info += " • \(shortHash)"
        }
        if !buildDate.isEmpty {
            info += " • \(buildDate)"
        }
        return info
    }
}