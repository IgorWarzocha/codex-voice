import Foundation

struct CodexCredentials {
    let accessToken: String
    let accountID: String
}

enum CodexAuthError: LocalizedError {
    case authFileMissing
    case credentialsUnavailable
    case refreshFailed(String)

    var errorDescription: String? {
        switch self {
        case .authFileMissing:
            return "Codex auth could not be found. Sign in to Codex first."
        case .credentialsUnavailable:
            return "Codex auth is incomplete. Sign in to Codex again."
        case .refreshFailed(let message):
            return "Codex auth refresh failed: \(message)"
        }
    }
}

actor CodexAuthService {
    private struct AuthFile: Decodable {
        struct Tokens: Decodable {
            let access_token: String
            let account_id: String
        }

        struct PiTokens: Decodable {
            let access: String
            let accountId: String
        }

        let tokens: Tokens?
        let openaiCodex: PiTokens?

        enum CodingKeys: String, CodingKey {
            case tokens
            case openaiCodex = "openai-codex"
        }
    }

    private let authFileURLs = [
        "~/.codex/auth.json",
        "~/.pi/agent/auth.json",
    ].map { URL(fileURLWithPath: NSString(string: $0).expandingTildeInPath) }

    func currentCredentials() throws -> CodexCredentials {
        try readCredentials()
    }

    func refreshCredentials() async throws -> CodexCredentials {
        try await refreshAuthState()
        return try readCredentials()
    }

    private func readCredentials() throws -> CodexCredentials {
        let existingURLs = authFileURLs.filter { FileManager.default.fileExists(atPath: $0.path) }
        guard !existingURLs.isEmpty else {
            throw CodexAuthError.authFileMissing
        }

        for authFileURL in existingURLs {
            let data = try Data(contentsOf: authFileURL)
            let authFile = try JSONDecoder().decode(AuthFile.self, from: data)
            let accessToken = authFile.tokens?.access_token ?? authFile.openaiCodex?.access
            let accountID = authFile.tokens?.account_id ?? authFile.openaiCodex?.accountId
            if let accessToken, let accountID, !accessToken.isEmpty, !accountID.isEmpty {
                return CodexCredentials(accessToken: accessToken, accountID: accountID)
            }
        }

        throw CodexAuthError.credentialsUnavailable
    }

    private func refreshAuthState() async throws {
        let codexBinaryURL = try resolveCodexBinaryURL()

        guard FileManager.default.isExecutableFile(atPath: codexBinaryURL.path) else {
            throw CodexAuthError.refreshFailed("Codex CLI was not found at \(codexBinaryURL.path).")
        }

        let process = Process()
        let inputPipe = Pipe()
        let outputPipe = Pipe()
        let errorPipe = Pipe()

        process.executableURL = codexBinaryURL
        process.arguments = ["app-server", "--listen", "stdio://"]
        process.standardInput = inputPipe
        process.standardOutput = outputPipe
        process.standardError = errorPipe

        try process.run()

        let messages: [[String: Any]] = [
            [
                "id": 1,
                "method": "initialize",
                "params": [
                    "clientInfo": [
                        "name": "codex-voice",
                        "version": "0.1.0",
                    ],
                    "capabilities": [
                        "experimentalApi": true,
                        "optOutNotificationMethods": [],
                    ],
                ],
            ],
            [
                "id": 2,
                "method": "account/read",
                "params": [
                    "refreshToken": true,
                ],
            ],
        ]

        for message in messages {
            let data = try JSONSerialization.data(withJSONObject: message)
            inputPipe.fileHandleForWriting.write(data)
            inputPipe.fileHandleForWriting.write(Data([0x0A]))
        }

        try inputPipe.fileHandleForWriting.close()

        let stdoutData = outputPipe.fileHandleForReading.readDataToEndOfFile()
        let stderrData = errorPipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()

        guard process.terminationStatus == 0 else {
            let message = String(data: stderrData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            throw CodexAuthError.refreshFailed(message?.isEmpty == false ? message! : "The Codex app-server exited with status \(process.terminationStatus).")
        }

        let output = String(decoding: stdoutData, as: UTF8.self)
        let lines = output.split(whereSeparator: \.isNewline)
        guard lines.contains(where: { $0.contains("\"id\":2") && $0.contains("\"result\"") }) else {
            throw CodexAuthError.refreshFailed("Codex did not confirm the auth refresh request.")
        }
    }

    private func resolveCodexBinaryURL() throws -> URL {
        let candidatePaths = [
            ProcessInfo.processInfo.environment["CODEX_CLI_PATH"],
            codexBinaryPathFromPATH(),
            "/Applications/Codex.app/Contents/Resources/codex",
        ].compactMap { $0 }

        if let executablePath = candidatePaths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) {
            return URL(fileURLWithPath: executablePath)
        }

        throw CodexAuthError.refreshFailed(
            """
            Codex CLI could not be found. Install Codex, make `codex` available on your PATH, \
            or set CODEX_CLI_PATH to the executable location.
            """
        )
    }

    private func codexBinaryPathFromPATH() -> String? {
        let process = Process()
        let outputPipe = Pipe()

        process.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        process.arguments = ["codex"]
        process.standardOutput = outputPipe
        process.standardError = Pipe()

        do {
            try process.run()
        } catch {
            return nil
        }

        let output = outputPipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()

        guard process.terminationStatus == 0 else {
            return nil
        }

        let path = String(decoding: output, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return path.isEmpty ? nil : path
    }
}
