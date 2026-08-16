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
                    try { relativeExe = File.ReadAllText(exeTxt).Trim(); }
                    catch { continue; }
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
            logger.Error(ex, $"GameDrive: failed to start Playnite library API on port {LibraryApiPort}.");
            libraryServer = null;
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
                ThreadPool.QueueUserWorkItem(_ => HandleLibraryRequest(client));
            }
            catch
            {
                break;
            }
        }
    }

    private void HandleLibraryRequest(TcpClient client)
    {
        using (client)
        using (NetworkStream stream = client.GetStream())
        {
            try
            {
                stream.ReadTimeout = 3000;
                stream.WriteTimeout = 3000;

                byte[] buffer = new byte[8192];
                int count = stream.Read(buffer, 0, buffer.Length);
                if (count <= 0) return;

                string request = Encoding.ASCII.GetString(buffer, 0, count);
                string firstLine = request.Split(new[] { "\r\n" }, StringSplitOptions.None)[0];
                string[] parts = firstLine.Split(' ');
                if (parts.Length < 2)
                {
                    WriteHttpJson(stream, 400, new Dictionary<string, object> { ["ok"] = false, ["error"] = "bad_request" });
                    return;
                }

                string path;
                try { path = Uri.UnescapeDataString(new Uri("http://127.0.0.1" + parts[1]).AbsolutePath).TrimEnd('/').ToLowerInvariant(); }
                catch { path = parts[1].ToLowerInvariant(); }

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
                    WriteHttpJson(stream, 200, new Dictionary<string, object>
                    {
                        ["ok"] = true,
                        ["ready"] = open,
                        ["source"] = "PlayniteApi.Database.Games",
                        ["gameCount"] = total,
                        ["installedCount"] = installed
                    });
                    return;
                }

                if (path == "/games")
                {
                    if (!api.Database.IsOpen)
                    {
                        WriteHttpJson(stream, 503, new Dictionary<string, object> { ["ok"] = false, ["error"] = "playnite_database_not_ready" });
                        return;
                    }

                    var result = api.Database.Games
                        .Where(g => g != null && g.IsInstalled)
                        .Select(ToApiGame)
                        .ToList();
                    WriteHttpJson(stream, 200, result);
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
                        WriteHttpJson(stream, 400, new Dictionary<string, object> { ["ok"] = false, ["error"] = "invalid_playnite_id" });
                        return;
                    }

                    var game = api.Database.Games.FirstOrDefault(g => g != null && g.Id == id && g.IsInstalled);
                    if (game == null)
                    {
                        WriteHttpJson(stream, 404, new Dictionary<string, object> { ["ok"] = false, ["error"] = "game_not_found" });
                        return;
                    }

                    try
                    {
                        api.StartGame(game.Id);
                        WriteHttpJson(stream, 200, new Dictionary<string, object> { ["ok"] = true, ["playniteId"] = game.Id.ToString() });
                    }
                    catch (Exception ex)
                    {
                        logger.Error(ex, $"GameDrive: failed to launch Playnite game {game.Id}");
                        WriteHttpJson(stream, 500, new Dictionary<string, object> { ["ok"] = false, ["error"] = "launch_failed", ["detail"] = ex.Message });
                    }
                    return;
                }

                WriteHttpJson(stream, 404, new Dictionary<string, object> { ["ok"] = false, ["error"] = "not_found" });
            }
            catch (Exception ex)
            {
                logger.Warn($"GameDrive: API request failed: {ex.Message}");
                try { WriteHttpJson(stream, 500, new Dictionary<string, object> { ["ok"] = false, ["error"] = "internal_error" }); } catch { }
            }
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

    private void WriteHttpJson(NetworkStream stream, int statusCode, object value)
    {
        string json;
        var serializer = new DataContractJsonSerializer(value.GetType());
        using (var ms = new MemoryStream())
        {
            serializer.WriteObject(ms, value);
            json = Encoding.UTF8.GetString(ms.ToArray());
        }

        byte[] body = Encoding.UTF8.GetBytes(json);
        string statusText = statusCode == 200 ? "OK" : statusCode == 400 ? "Bad Request" : statusCode == 404 ? "Not Found" : statusCode == 409 ? "Conflict" : statusCode == 500 ? "Internal Server Error" : "Service Unavailable";
        string headers = $"HTTP/1.1 {statusCode} {statusText}\r\nContent-Type: application/json; charset=utf-8\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n";
        byte[] headerBytes = Encoding.ASCII.GetBytes(headers);
        stream.Write(headerBytes, 0, headerBytes.Length);
        stream.Write(body, 0, body.Length);
        stream.Flush();
    }
}
