using IniParser;
using IniParser.Model;
using Playnite.SDK;
using Playnite.SDK.Events;
using Playnite.SDK.Models;
using Playnite.SDK.Plugins;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading;

public class GameDriveLibrary : LibraryPlugin
{
    private readonly IPlayniteAPI api;
    private static readonly ILogger logger = LogManager.GetLogger();
    private TcpListener libraryServer;
    private Thread libraryServerThread;
    private const int LibraryApiPort = 38123;
    private volatile bool libraryReady;

    public override Guid Id { get; } = Guid.Parse("9c8e41d2-18cb-40d3-b5a3-3b97766d0101");
    public override string Name => "GameDrive";

    public GameDriveLibrary(IPlayniteAPI api) : base(api) => this.api = api;

    public override void OnApplicationStarted(OnApplicationStartedEventArgs args)
    {
        libraryReady = false;
        StartLibraryApi();
        TryUpdateReadyState();
    }

    public override void OnLibraryUpdated(OnLibraryUpdatedEventArgs args)
    {
        TryUpdateReadyState();
    }

    public override void OnApplicationStopped(OnApplicationStoppedEventArgs args)
    {
        libraryReady = false;
        StopLibraryApi();
    }

    private void TryUpdateReadyState()
    {
        try
        {
            bool open = api.Database.IsOpen;
            int installed = open ? api.Database.Games.Count(g => g != null && g.IsInstalled) : 0;
            libraryReady = open;
            logger.Info($"GameDrive: Playnite API ready={libraryReady}, installed games={installed}");
        }
        catch (Exception ex)
        {
            libraryReady = false;
            logger.Warn($"GameDrive: Playnite database is not ready: {ex.Message}");
        }
    }

    public override IEnumerable<GameMetadata> GetGames(LibraryGetGamesArgs args)
    {
        var games = new List<GameMetadata>();
        foreach (var drive in DriveInfo.GetDrives().Where(d => d.IsReady))
        {
            string iniPath = Path.Combine(drive.RootDirectory.FullName, "GameDrive.ini");
            if (!File.Exists(iniPath)) continue;
            IniData ini;
            try { ini = new FileIniDataParser().ReadFile(iniPath); }
            catch (Exception ex) { logger.Error(ex, $"GameDrive: failed to parse {iniPath}"); continue; }
            if (!ini.Sections.ContainsSection("Directories")) continue;
            foreach (var key in ini["Directories"])
            {
                string folder = Path.Combine(drive.RootDirectory.FullName, key.Value);
                if (!Directory.Exists(folder)) continue;
                foreach (string gameFolder in Directory.GetDirectories(folder))
                {
                    string name = Path.GetFileName(gameFolder);
                    string exeTxt = Path.Combine(gameFolder, "exepath.txt");
                    if (!File.Exists(exeTxt)) continue;
                    string relativeExe;
                    try { relativeExe = File.ReadAllText(exeTxt).Trim(); } catch { continue; }
                    string exe = Path.Combine(gameFolder, "Game", relativeExe);
                    if (!File.Exists(exe)) continue;
                    games.Add(new GameMetadata
                    {
                        Name = name,
                        GameId = name,
                        InstallDirectory = Path.Combine(gameFolder, "Game"),
                        IsInstalled = true,
                        GameActions = new List<GameAction> { new GameAction { Name = "Play", Type = GameActionType.File, Path = exe, IsPlayAction = true } },
                        Categories = new HashSet<MetadataProperty>()
                    });
                }
            }
        }
        return games;
    }

    private void StartLibraryApi()
    {
        if (libraryServer != null) return;
        try
        {
            libraryServer = new TcpListener(IPAddress.Loopback, LibraryApiPort);
            libraryServer.Start();
            libraryServerThread = new Thread(LibraryApiLoop) { IsBackground = true, Name = "GameDrive Playnite API" };
            libraryServerThread.Start();
            logger.Info($"GameDrive: Playnite library API listening on http://127.0.0.1:{LibraryApiPort}/");
        }
        catch (Exception ex)
        {
            libraryServer = null;
            logger.Error(ex, "GameDrive: failed to start Playnite library API.");
        }
    }

    private void StopLibraryApi()
    {
        try { if (libraryServer != null) libraryServer.Stop(); } catch { }
        libraryServer = null;
        libraryServerThread = null;
    }

    private void LibraryApiLoop()
    {
        while (libraryServer != null)
        {
            try
            {
                TcpClient client = libraryServer.AcceptTcpClient();
                ThreadPool.QueueUserWorkItem(_ => HandleClient(client));
            }
            catch { break; }
        }
    }

    private void HandleClient(TcpClient client)
    {
        using (client)
        using (NetworkStream stream = client.GetStream())
        {
            try
            {
                stream.ReadTimeout = 3000;
                stream.WriteTimeout = 3000;
                string request = ReadHttpRequest(stream);
                if (string.IsNullOrEmpty(request)) return;
                string firstLine = request.Split(new[] { "\r\n" }, StringSplitOptions.None)[0];
                string[] parts = firstLine.Split(' ');
                string path = parts.Length >= 2 ? parts[1].Split('?')[0].TrimEnd('/').ToLowerInvariant() : "";
                HandleApiRequest(stream, path);
            }
            catch (Exception ex)
            {
                logger.Warn($"GameDrive: API client failed: {ex.Message}");
            }
        }
    }

    private string ReadHttpRequest(NetworkStream stream)
    {
        var buffer = new byte[4096];
        using (var ms = new MemoryStream())
        {
            while (ms.Length < 32768)
            {
                int read = stream.Read(buffer, 0, buffer.Length);
                if (read <= 0) break;
                ms.Write(buffer, 0, read);
                string text = Encoding.ASCII.GetString(ms.ToArray());
                if (text.IndexOf("\r\n\r\n", StringComparison.Ordinal) >= 0) return text;
            }
            return Encoding.ASCII.GetString(ms.ToArray());
        }
    }

    private void HandleApiRequest(NetworkStream stream, string path)
    {
        try
        {
            if (path == "/health")
            {
                int total = 0, installed = 0;
                bool open = false;
                try
                {
                    open = api.Database.IsOpen;
                    if (open)
                    {
                        total = api.Database.Games.Count;
                        installed = api.Database.Games.Count(g => g != null && g.IsInstalled);
                    }
                }
                catch { open = false; }
                libraryReady = open;
                WriteJson(stream, new Dictionary<string, object>
                {
                    ["ok"] = true,
                    ["ready"] = open,
                    ["source"] = "PlayniteApi.Database.Games",
                    ["gameCount"] = total,
                    ["installedCount"] = installed
                }, 200);
                return;
            }

            if (path == "/games")
            {
                if (!api.Database.IsOpen)
                {
                    WriteJson(stream, new Dictionary<string, object> { ["ok"] = false, ["error"] = "playnite_database_not_ready" }, 503);
                    return;
                }
                var result = api.Database.Games.Where(g => g != null && g.IsInstalled).Select(ToApiGame).ToList();
                WriteJson(stream, result, 200);
                return;
            }

            const string launchPrefix = "/games/";
            const string launchSuffix = "/launch";
            if (path.StartsWith(launchPrefix) && path.EndsWith(launchSuffix))
            {
                string idText = path.Substring(launchPrefix.Length, path.Length - launchPrefix.Length - launchSuffix.Length);
                Guid id;
                if (!Guid.TryParse(idText, out id))
                {
                    WriteJson(stream, new Dictionary<string, object> { ["ok"] = false, ["error"] = "invalid_playnite_id" }, 400);
                    return;
                }
                var game = api.Database.Games.FirstOrDefault(g => g != null && g.Id == id && g.IsInstalled);
                if (game == null)
                {
                    WriteJson(stream, new Dictionary<string, object> { ["ok"] = false, ["error"] = "game_not_found" }, 404);
                    return;
                }
                try
                {
                    api.StartGame(game.Id);
                    WriteJson(stream, new Dictionary<string, object> { ["ok"] = true, ["playniteId"] = game.Id.ToString() }, 200);
                }
                catch (Exception ex)
                {
                    logger.Error(ex, $"GameDrive: failed to launch Playnite game {game.Id}");
                    WriteJson(stream, new Dictionary<string, object> { ["ok"] = false, ["error"] = "launch_failed", ["detail"] = ex.Message }, 500);
                }
                return;
            }

            WriteJson(stream, new Dictionary<string, object> { ["ok"] = false, ["error"] = "not_found" }, 404);
        }
        catch (Exception ex)
        {
            logger.Warn($"GameDrive: API request failed: {ex.Message}");
            try { WriteJson(stream, new Dictionary<string, object> { ["ok"] = false, ["error"] = "internal_error" }, 500); } catch { }
        }
    }

    private Dictionary<string, object> ToApiGame(Game game)
    {
        var action = game.GameActions?.FirstOrDefault(a => a != null && a.IsPlayAction) ?? game.GameActions?.FirstOrDefault();
        return new Dictionary<string, object>
        {
            ["id"] = game.Id.ToString(),
            ["name"] = game.Name ?? "Unknown Game",
            ["gameId"] = game.GameId,
            ["sourceId"] = game.SourceId,
            ["isInstalled"] = game.IsInstalled,
            ["installDirectory"] = game.InstallDirectory,
            ["executable"] = action?.Path,
            ["arguments"] = action?.Arguments,
            ["workingDirectory"] = action?.WorkingDir,
            ["description"] = game.Description,
            ["releaseDate"] = game.ReleaseDate,
            ["playtime"] = game.Playtime,
            ["cover"] = game.CoverImage,
            ["hero"] = game.BackgroundImage,
            ["logo"] = game.Icon
        };
    }

    private void WriteJson(NetworkStream stream, object value, int statusCode)
    {
        string json;
        var serializer = new DataContractJsonSerializer(value.GetType());
        using (var ms = new MemoryStream())
        {
            serializer.WriteObject(ms, value);
            json = Encoding.UTF8.GetString(ms.ToArray());
        }
        byte[] body = Encoding.UTF8.GetBytes(json);
        string status = statusCode == 200 ? "OK" : statusCode == 400 ? "Bad Request" : statusCode == 404 ? "Not Found" : statusCode == 409 ? "Conflict" : "Service Unavailable";
        string header = "HTTP/1.1 " + statusCode + " " + status + "\r\n" +
                        "Content-Type: application/json; charset=utf-8\r\n" +
                        "Content-Length: " + body.Length + "\r\n" +
                        "Access-Control-Allow-Origin: *\r\n" +
                        "Connection: close\r\n\r\n";
        byte[] head = Encoding.ASCII.GetBytes(header);
        stream.Write(head, 0, head.Length);
        stream.Write(body, 0, body.Length);
        stream.Flush();
    }
}
