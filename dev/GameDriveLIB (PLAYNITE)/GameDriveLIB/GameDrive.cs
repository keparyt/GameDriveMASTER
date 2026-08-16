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

[DataContract]
public sealed class GameDriveHealthResponse
{
    [DataMember(Name = "ok")] public bool Ok { get; set; }
    [DataMember(Name = "ready")] public bool Ready { get; set; }
    [DataMember(Name = "source")] public string Source { get; set; }
    [DataMember(Name = "gameCount")] public int GameCount { get; set; }
    [DataMember(Name = "installedCount")] public int InstalledCount { get; set; }
}

[DataContract]
public sealed class GameDriveApiGame
{
    [DataMember(Name = "id")] public string Id { get; set; }
    [DataMember(Name = "name")] public string Name { get; set; }
    [DataMember(Name = "gameId")] public string GameId { get; set; }
    [DataMember(Name = "sourceId")] public string SourceId { get; set; }
    [DataMember(Name = "isInstalled")] public bool IsInstalled { get; set; }
    [DataMember(Name = "installDirectory")] public string InstallDirectory { get; set; }
    [DataMember(Name = "executable")] public string Executable { get; set; }
    [DataMember(Name = "arguments")] public string Arguments { get; set; }
    [DataMember(Name = "workingDirectory")] public string WorkingDirectory { get; set; }
    [DataMember(Name = "description")] public string Description { get; set; }
    [DataMember(Name = "releaseDate")] public string ReleaseDate { get; set; }
    [DataMember(Name = "playtime")] public ulong Playtime { get; set; }
    [DataMember(Name = "cover")] public string Cover { get; set; }
    [DataMember(Name = "hero")] public string Hero { get; set; }
    [DataMember(Name = "logo")] public string Logo { get; set; }
}

[DataContract]
public sealed class GameDriveLaunchResponse
{
    [DataMember(Name = "ok")] public bool Ok { get; set; }
    [DataMember(Name = "playniteId", EmitDefaultValue = false)] public string PlayniteId { get; set; }
    [DataMember(Name = "error", EmitDefaultValue = false)] public string Error { get; set; }
    [DataMember(Name = "detail", EmitDefaultValue = false)] public string Detail { get; set; }
}

[DataContract]
public sealed class GameDriveErrorResponse
{
    [DataMember(Name = "ok")] public bool Ok { get; set; }
    [DataMember(Name = "error")] public string Error { get; set; }
}

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

    public override void OnLibraryUpdated(OnLibraryUpdatedEventArgs args) => TryUpdateReadyState();

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
                stream.ReadTimeout = 5000;
                stream.WriteTimeout = 5000;
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
                WriteJson(stream, new GameDriveHealthResponse
                {
                    Ok = true,
                    Ready = open,
                    Source = "PlayniteApi.Database.Games",
                    GameCount = total,
                    InstalledCount = installed
                }, 200);
                return;
            }

            if (path == "/games")
            {
                if (!api.Database.IsOpen)
                {
                    WriteJson(stream, new GameDriveErrorResponse { Ok = false, Error = "playnite_database_not_ready" }, 503);
                    return;
                }

                var result = api.Database.Games
                    .Where(g => g != null && g.IsInstalled)
                    .Select(ToApiGame)
                    .ToList();

                logger.Info($"GameDrive: serving {result.Count} installed Playnite game(s) through /games");
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
                    WriteJson(stream, new GameDriveErrorResponse { Ok = false, Error = "invalid_playnite_id" }, 400);
                    return;
                }

                var game = api.Database.Games.FirstOrDefault(g => g != null && g.Id == id && g.IsInstalled);
                if (game == null)
                {
                    WriteJson(stream, new GameDriveErrorResponse { Ok = false, Error = "game_not_found" }, 404);
                    return;
                }

                try
                {
                    api.StartGame(game.Id);
                    WriteJson(stream, new GameDriveLaunchResponse { Ok = true, PlayniteId = game.Id.ToString() }, 200);
                }
                catch (Exception ex)
                {
                    logger.Error(ex, $"GameDrive: failed to launch Playnite game {game.Id}");
                    WriteJson(stream, new GameDriveLaunchResponse { Ok = false, Error = "launch_failed", Detail = ex.Message }, 500);
                }
                return;
            }

            WriteJson(stream, new GameDriveErrorResponse { Ok = false, Error = "not_found" }, 404);
        }
        catch (Exception ex)
        {
            logger.Warn($"GameDrive: API request failed: {ex.Message}");
            try { WriteJson(stream, new GameDriveErrorResponse { Ok = false, Error = "internal_error" }, 500); } catch { }
        }
    }

    private GameDriveApiGame ToApiGame(Game game)
    {
        var action = game.GameActions?.FirstOrDefault(a => a != null && a.IsPlayAction) ?? game.GameActions?.FirstOrDefault();
        return new GameDriveApiGame
        {
            Id = game.Id.ToString(),
            Name = game.Name ?? "Unknown Game",
            GameId = game.GameId == null ? null : Convert.ToString(game.GameId),
            SourceId = game.SourceId == null ? null : Convert.ToString(game.SourceId),
            IsInstalled = game.IsInstalled,
            InstallDirectory = game.InstallDirectory == null ? null : Convert.ToString(game.InstallDirectory),
            Executable = action?.Path == null ? null : Convert.ToString(action.Path),
            Arguments = action?.Arguments == null ? null : Convert.ToString(action.Arguments),
            WorkingDirectory = action?.WorkingDir == null ? null : Convert.ToString(action.WorkingDir),
            Description = game.Description,
            ReleaseDate = game.ReleaseDate.HasValue ? Convert.ToString(game.ReleaseDate.Value) : null,
            Playtime = game.Playtime,
            Cover = game.CoverImage,
            Hero = game.BackgroundImage,
            Logo = game.Icon
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
        string status = statusCode == 200 ? "OK" : statusCode == 400 ? "Bad Request" : statusCode == 404 ? "Not Found" : statusCode == 409 ? "Conflict" : statusCode == 500 ? "Internal Server Error" : "Service Unavailable";
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
