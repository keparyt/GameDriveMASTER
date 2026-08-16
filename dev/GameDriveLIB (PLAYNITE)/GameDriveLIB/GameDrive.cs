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
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading;

public class GameDriveLibrary : LibraryPlugin
{
    private readonly IPlayniteAPI api;
    private static readonly ILogger logger = LogManager.GetLogger();
    private readonly HttpListener libraryServer = new HttpListener();
    private Thread libraryServerThread;
    private const string LibraryApiPrefix = "http://127.0.0.1:38123/";
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
        if (libraryServer.IsListening) return;
        try
        {
            libraryServer.Prefixes.Clear();
            libraryServer.Prefixes.Add(LibraryApiPrefix);
            libraryServer.Start();
            libraryServerThread = new Thread(LibraryApiLoop) { IsBackground = true, Name = "GameDrive Playnite API" };
            libraryServerThread.Start();
            logger.Info($"GameDrive: Playnite library API listening on {LibraryApiPrefix}");
        }
        catch (Exception ex)
        {
            logger.Error(ex, "GameDrive: failed to start Playnite library API.");
        }
    }

    private void StopLibraryApi()
    {
        try { if (libraryServer.IsListening) libraryServer.Stop(); } catch { }
        try { libraryServer.Close(); } catch { }
        libraryServerThread = null;
    }

    private void LibraryApiLoop()
    {
        while (libraryServer.IsListening)
        {
            try
            {
                var context = libraryServer.GetContext();
                ThreadPool.QueueUserWorkItem(_ => HandleLibraryRequest(context));
            }
            catch { break; }
        }
    }

    private void HandleLibraryRequest(HttpListenerContext context)
    {
        try
        {
            context.Response.Headers["Access-Control-Allow-Origin"] = "*";
            string path = context.Request.Url.AbsolutePath.TrimEnd('/').ToLowerInvariant();

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
                WriteJson(context, new Dictionary<string, object>
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
                    WriteJson(context, new Dictionary<string, object> { ["ok"] = false, ["error"] = "playnite_database_not_ready" }, 503);
                    return;
                }

                var result = api.Database.Games
                    .Where(g => g != null && g.IsInstalled)
                    .Select(ToApiGame)
                    .ToList();
                WriteJson(context, result);
                return;
            }

            const string launchPrefix = "/games/";
            const string launchSuffix = "/launch";
            if (path.StartsWith(launchPrefix) && path.EndsWith(launchSuffix))
            {
                string idText = path.Substring(launchPrefix.Length, path.Length - launchPrefix.Length - launchSuffix.Length);
                if (!Guid.TryParse(idText, out Guid id))
                {
                    WriteJson(context, new Dictionary<string, object> { ["ok"] = false, ["error"] = "invalid_playnite_id" }, 400);
                    return;
                }

                var game = api.Database.Games.FirstOrDefault(g => g != null && g.Id == id && g.IsInstalled);
                if (game == null)
                {
                    WriteJson(context, new Dictionary<string, object> { ["ok"] = false, ["error"] = "game_not_found" }, 404);
                    return;
                }

                var action = game.GameActions?.FirstOrDefault(a => a != null && a.IsPlayAction) ?? game.GameActions?.FirstOrDefault();
                if (action == null)
                {
                    WriteJson(context, new Dictionary<string, object> { ["ok"] = false, ["error"] = "no_game_action" }, 409);
                    return;
                }

                try
                {
                    api.StartGame(game.Id);
                    WriteJson(context, new Dictionary<string, object> { ["ok"] = true, ["playniteId"] = game.Id.ToString() });
                }
                catch (Exception ex)
                {
                    logger.Error(ex, $"GameDrive: failed to launch Playnite game {game.Id}");
                    WriteJson(context, new Dictionary<string, object> { ["ok"] = false, ["error"] = "launch_failed", ["detail"] = ex.Message }, 500);
                }
                return;
            }

            WriteJson(context, new Dictionary<string, object> { ["ok"] = false, ["error"] = "not_found" }, 404);
        }
        catch (Exception ex)
        {
            logger.Warn($"GameDrive: API request failed: {ex.Message}");
            try { WriteJson(context, new Dictionary<string, object> { ["ok"] = false, ["error"] = "internal_error" }, 500); } catch { }
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
            ["logo"] = game.Icon,
        };
    }

    private void WriteJson(HttpListenerContext context, object value, int statusCode = 200)
    {
        string json;
        var serializer = new DataContractJsonSerializer(value.GetType());
        using (var ms = new MemoryStream())
        {
            serializer.WriteObject(ms, value);
            json = Encoding.UTF8.GetString(ms.ToArray());
        }
        byte[] bytes = Encoding.UTF8.GetBytes(json);
        context.Response.StatusCode = statusCode;
        context.Response.ContentType = "application/json; charset=utf-8";
        context.Response.ContentLength64 = bytes.Length;
        using (var stream = context.Response.OutputStream) stream.Write(bytes, 0, bytes.Length);
    }
}
