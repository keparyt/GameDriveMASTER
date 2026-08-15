using IniParser;
using IniParser.Model;
using Playnite.SDK;
using Playnite.SDK.Models;
using Playnite.SDK.Plugins;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
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

    public override Guid Id { get; } =
        Guid.Parse("9c8e41d2-18cb-40d3-b5a3-3b97766d0101");

    public override string Name => "GameDrive";

    public GameDriveLibrary(IPlayniteAPI api) : base(api)
    {
        this.api = api;
        StartLibraryApi();
    }

    public override IEnumerable<GameMetadata> GetGames(LibraryGetGamesArgs args)
    {
        var games = new List<GameMetadata>();

        var drives = DriveInfo.GetDrives().Where(d => d.IsReady).ToList();
        logger.Info($"GameDrive: scanning {drives.Count} ready drive(s): {string.Join(", ", drives.Select(d => d.Name))}");

        foreach (var drive in drives)
        {
            string iniPath = Path.Combine(drive.RootDirectory.FullName, "GameDrive.ini");

            if (!File.Exists(iniPath))
            {
                logger.Info($"GameDrive: no GameDrive.ini on {drive.Name}, skipping.");
                continue;
            }

            logger.Info($"GameDrive: found GameDrive.ini on {drive.Name}");

            IniData ini;
            try
            {
                var parser = new FileIniDataParser();
                ini = parser.ReadFile(iniPath);
            }
            catch (Exception ex)
            {
                logger.Error(ex, $"GameDrive: failed to parse {iniPath}");
                continue;
            }

            if (!ini.Sections.ContainsSection("Directories"))
            {
                logger.Warn($"GameDrive: {iniPath} has no [Directories] section.");
                continue;
            }

            string apiKey = null;
            if (ini.Sections.ContainsSection("SteamGridDB"))
                apiKey = ini["SteamGridDB"]["api_key"];

            SteamGridDbClient sgdb = null;
            if (!string.IsNullOrWhiteSpace(apiKey))
                sgdb = new SteamGridDbClient(apiKey.Trim(), logger);
            else
                logger.Info("GameDrive: SteamGridDB API key is not configured; artwork download is disabled.");

            foreach (var key in ini["Directories"])
            {
                string folder = Path.Combine(drive.RootDirectory.FullName, key.Value);

                if (!Directory.Exists(folder))
                {
                    logger.Warn($"GameDrive: directory '{folder}' listed in ini but does not exist.");
                    continue;
                }

                foreach (string gameFolder in Directory.GetDirectories(folder))
                {
                    string name = Path.GetFileName(gameFolder);
                    string exeTxt = Path.Combine(gameFolder, "exepath.txt");
                    if (!File.Exists(exeTxt))
                        continue;

                    string relativeExe;
                    try
                    {
                        relativeExe = File.ReadAllText(exeTxt).Trim();
                    }
                    catch (Exception ex)
                    {
                        logger.Error(ex, $"GameDrive: could not read {exeTxt}");
                        continue;
                    }

                    string exe = Path.Combine(gameFolder, "Game", relativeExe);
                    if (!File.Exists(exe))
                        continue;

                    var metadata = new GameMetadata
                    {
                        Name = name,
                        GameId = name,
                        InstallDirectory = Path.Combine(gameFolder, "Game"),
                        IsInstalled = true,
                        Categories = new HashSet<MetadataProperty>()
                    };

                    metadata.GameActions = new List<GameAction>
                    {
                        new GameAction
                        {
                            Name = "Play",
                            Type = GameActionType.File,
                            Path = exe,
                            IsPlayAction = true
                        }
                    };

                    if (sgdb != null)
                        EnsureMediaAssets(gameFolder, name, metadata, sgdb, logger);
                    else
                        ApplyExistingMedia(gameFolder, metadata);

                    games.Add(metadata);
                }
            }
        }

        logger.Info($"GameDrive: scan complete, {games.Count} game(s) found.");
        return games;
    }

    private void StartLibraryApi()
    {
        try
        {
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

    private void LibraryApiLoop()
    {
        while (libraryServer.IsListening)
        {
            try
            {
                var context = libraryServer.GetContext();
                ThreadPool.QueueUserWorkItem(_ => HandleLibraryRequest(context));
            }
            catch (HttpListenerException)
            {
                break;
            }
            catch (ObjectDisposedException)
            {
                break;
            }
            catch (Exception ex)
            {
                logger.Warn($"GameDrive: Playnite API listener error: {ex.Message}");
            }
        }
    }

    private void HandleLibraryRequest(HttpListenerContext context)
    {
        try
        {
            string path = context.Request.Url.AbsolutePath.TrimEnd('/').ToLowerInvariant();

            if (path == "/health")
            {
                WriteJson(context, new { ok = true, source = "PlayniteApi.Database.Games" });
                return;
            }

            if (path == "/games" && context.Request.HttpMethod == "GET")
            {
                // This is the authoritative Playnite library. The service must not read games.db itself.
                var games = api.Database.Games
                    .Where(g => g != null && g.IsInstalled)
                    .Select(ToLibraryGame)
                    .ToList();

                WriteJson(context, games);
                return;
            }

            WriteJson(context, new { ok = false, error = "not_found" }, 404);
        }
        catch (Exception ex)
        {
            logger.Error(ex, "GameDrive: Playnite library API request failed.");
            try
            {
                WriteJson(context, new { ok = false, error = ex.Message }, 500);
            }
            catch { }
        }
    }

    private object ToLibraryGame(Game game)
    {
        GameAction action = null;
        if (game.GameActions != null)
            action = game.GameActions.FirstOrDefault(a => a != null && a.IsPlayAction) ?? game.GameActions.FirstOrDefault();

        return new
        {
            id = game.Id,
            name = game.Name,
            gameId = game.GameId,
            sourceId = game.SourceId,
            isInstalled = game.IsInstalled,
            installDirectory = game.InstallDirectory,
            executable = action?.Path,
            arguments = action?.Arguments,
            workingDirectory = action?.WorkingDir,
            description = game.Description,
            releaseDate = game.ReleaseDate,
            playtime = game.Playtime
        };
    }

    private void WriteJson(HttpListenerContext context, object value, int statusCode = 200)
    {
        byte[] bytes;
        var serializer = new DataContractJsonSerializer(value.GetType());
        using (var ms = new MemoryStream())
        {
            serializer.WriteObject(ms, value);
            bytes = ms.ToArray();
        }

        context.Response.StatusCode = statusCode;
        context.Response.ContentType = "application/json; charset=utf-8";
        context.Response.ContentLength64 = bytes.Length;
        context.Response.OutputStream.Write(bytes, 0, bytes.Length);
        context.Response.OutputStream.Close();
    }

    private void EnsureMediaAssets(string gameFolder, string gameName, GameMetadata metadata, SteamGridDbClient sgdb, ILogger logger)
    {
        string capsulePath = Path.Combine(gameFolder, "Capsule.png");
        string heroPath = Path.Combine(gameFolder, "Hero.png");
        string iconPath = Path.Combine(gameFolder, "Icon.png");
        bool needCapsule = !File.Exists(capsulePath);
        bool needHero = !File.Exists(heroPath);
        bool needIcon = !File.Exists(iconPath);
        int? gameId = null;

        if (needCapsule || needHero || needIcon)
        {
            try { gameId = sgdb.SearchGameId(gameName); }
            catch (Exception ex) { logger.Warn($"GameDrive: SteamGridDB search failed for '{gameName}': {ex.Message}"); }

            if (gameId != null)
            {
                if (needCapsule) TryDownload(sgdb, "grids", gameId.Value, "600x900", capsulePath, logger, "capsule");
                if (needHero) TryDownload(sgdb, "heroes", gameId.Value, "1920x620", heroPath, logger, "hero/background");
                if (needIcon) TryDownload(sgdb, "icons", gameId.Value, null, iconPath, logger, "icon");
            }
        }

        ApplyExistingMedia(gameFolder, metadata);
    }

    private void ApplyExistingMedia(string gameFolder, GameMetadata metadata)
    {
        string capsulePath = Path.Combine(gameFolder, "Capsule.png");
        string heroPath = Path.Combine(gameFolder, "Hero.png");
        string iconPath = Path.Combine(gameFolder, "Icon.png");
        if (File.Exists(capsulePath)) metadata.CoverImage = new MetadataFile(capsulePath);
        if (File.Exists(heroPath)) metadata.BackgroundImage = new MetadataFile(heroPath);
        if (File.Exists(iconPath)) metadata.Icon = new MetadataFile(iconPath);
    }

    private void TryDownload(SteamGridDbClient sgdb, string category, int gameId, string dimensions, string destPath, ILogger logger, string label)
    {
        try
        {
            string url = sgdb.GetImageUrl(category, gameId, dimensions);
            if (string.IsNullOrWhiteSpace(url)) return;
            sgdb.DownloadFile(url, destPath);
        }
        catch (Exception ex)
        {
            logger.Warn($"GameDrive: failed to download {label} for game id {gameId}: {ex.Message}");
        }
    }
}

public class SteamGridDbClient
{
    private static readonly HttpClient http = CreateHttpClient();
    private readonly string apiKey;
    private readonly ILogger logger;

    public SteamGridDbClient(string apiKey, ILogger logger) { this.apiKey = apiKey; this.logger = logger; }

    private static HttpClient CreateHttpClient()
    {
        var client = new HttpClient();
        client.Timeout = TimeSpan.FromSeconds(20);
        client.DefaultRequestHeaders.UserAgent.ParseAdd("GameDrive/1.0");
        return client;
    }

    private HttpRequestMessage BuildRequest(string url)
    {
        var request = new HttpRequestMessage(HttpMethod.Get, url);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", apiKey);
        return request;
    }

    private T SendAndParse<T>(string url)
    {
        using (var request = BuildRequest(url))
        using (var response = http.SendAsync(request).GetAwaiter().GetResult())
        {
            response.EnsureSuccessStatusCode();
            using (var stream = response.Content.ReadAsStreamAsync().GetAwaiter().GetResult())
            {
                var serializer = new DataContractJsonSerializer(typeof(T));
                return (T)serializer.ReadObject(stream);
            }
        }
    }

    public int? SearchGameId(string gameName)
    {
        string url = "https://www.steamgriddb.com/api/v2/search/autocomplete/" + Uri.EscapeDataString(gameName);
        var result = SendAndParse<SgdbListResponse<SgdbGame>>(url);
        if (result?.Data == null || result.Data.Count == 0) return null;
        var exact = result.Data.FirstOrDefault(x => string.Equals(Normalize(x.Name), Normalize(gameName), StringComparison.OrdinalIgnoreCase));
        return (exact ?? result.Data[0]).Id;
    }

    private static string Normalize(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return string.Empty;
        return new string(value.Where(char.IsLetterOrDigit).ToArray()).ToLowerInvariant();
    }

    public string GetImageUrl(string category, int gameId, string dimensions)
    {
        string url = $"https://www.steamgriddb.com/api/v2/{category}/game/{gameId}?mimes=image/png";
        if (!string.IsNullOrEmpty(dimensions)) url += "&dimensions=" + Uri.EscapeDataString(dimensions);
        var result = SendAndParse<SgdbListResponse<SgdbImage>>(url);
        return result?.Data != null && result.Data.Count > 0 ? result.Data[0].Url : null;
    }

    public void DownloadFile(string url, string destPath)
    {
        using (var response = http.GetAsync(url).GetAwaiter().GetResult())
        {
            response.EnsureSuccessStatusCode();
            var bytes = response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult();
            string directory = Path.GetDirectoryName(destPath);
            if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
            string tempPath = destPath + ".tmp";
            File.WriteAllBytes(tempPath, bytes);
            if (File.Exists(destPath)) File.Delete(destPath);
            File.Move(tempPath, destPath);
        }
    }
}

[DataContract]
public class SgdbListResponse<T>
{
    [DataMember(Name = "success")] public bool Success { get; set; }
    [DataMember(Name = "data")] public List<T> Data { get; set; }
}

[DataContract]
public class SgdbGame
{
    [DataMember(Name = "id")] public int Id { get; set; }
    [DataMember(Name = "name")] public string Name { get; set; }
}

[DataContract]
public class SgdbImage
{
    [DataMember(Name = "id")] public int Id { get; set; }
    [DataMember(Name = "url")] public string Url { get; set; }
}
