using IniParser;
using IniParser.Model;
using Playnite.SDK;
using Playnite.SDK.Models;
using Playnite.SDK.Plugins;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;

public class GameDriveLibrary : LibraryPlugin
{
    private readonly IPlayniteAPI api;
    private static readonly ILogger logger = LogManager.GetLogger();

    // Fallback key used if GameDrive.ini doesn't specify one under [SteamGridDB] api_key=...
    // For safety/quota reasons, put your own key in the ini instead of relying on this default.
    private const string DefaultSteamGridDbKey = "a2bc700c7af45388526300fcf8b6c7ab";

    public override Guid Id { get; } =
        Guid.Parse("9c8e41d2-18cb-40d3-b5a3-3b97766d0101");

    public override string Name => "GameDrive";

    public GameDriveLibrary(IPlayniteAPI api) : base(api)
    {
        this.api = api;
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

            string apiKey = DefaultSteamGridDbKey;
            if (ini.Sections.ContainsSection("SteamGridDB") && !string.IsNullOrWhiteSpace(ini["SteamGridDB"]["api_key"]))
            {
                apiKey = ini["SteamGridDB"]["api_key"];
            }
            var sgdb = new SteamGridDbClient(apiKey, logger);

            foreach (var key in ini["Directories"])
            {
                string folder = Path.Combine(drive.RootDirectory.FullName, key.Value);

                if (!Directory.Exists(folder))
                {
                    logger.Warn($"GameDrive: directory '{folder}' listed in ini but does not exist.");
                    continue;
                }

                logger.Info($"GameDrive: scanning folder '{folder}'");

                foreach (string gameFolder in Directory.GetDirectories(folder))
                {
                    string name = Path.GetFileName(gameFolder);

                    string exeTxt = Path.Combine(gameFolder, "exepath.txt");
                    if (!File.Exists(exeTxt))
                    {
                        logger.Warn($"GameDrive: '{name}' has no exepath.txt, skipping.");
                        continue;
                    }

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
                    {
                        logger.Warn($"GameDrive: '{name}' exe not found at '{exe}' (exepath.txt said '{relativeExe}').");
                        continue;
                    }

                    logger.Info($"GameDrive: found game '{name}' -> {exe}");

                    var metadata = new GameMetadata
                    {
                        Name = name,
                        GameId = name,
                        InstallDirectory = Path.Combine(gameFolder, "Game"),
                        IsInstalled = true
                    };

                    metadata.GameActions = new List<GameAction>()
                    {
                        new GameAction()
                        {
                            Name = "Play",
                            Type = GameActionType.File,
                            Path = exe,
                            IsPlayAction = true
                        }
                    };

                    EnsureMediaAssets(gameFolder, name, metadata, sgdb, logger);

                    metadata.Categories = new HashSet<MetadataProperty>();

                    games.Add(metadata);
                }
            }
        }

        logger.Info($"GameDrive: scan complete, {games.Count} game(s) found.");
        return games;
    }

    // Downloads Capsule.png (cover), Hero.png (widescreen background) and Icon.png
    // for a game from SteamGridDB if they aren't already cached in the game folder,
    // then wires them into the game's metadata. Each asset is only fetched once;
    // once a file exists locally, it's reused on every future scan.
    private void EnsureMediaAssets(string gameFolder, string gameName, GameMetadata metadata, SteamGridDbClient sgdb, ILogger logger)
    {
        string capsulePath = Path.Combine(gameFolder, "Capsule.png");   // portrait cover, used in grid view
        string heroPath = Path.Combine(gameFolder, "Hero.png");        // widescreen background, used in full screen mode
        string iconPath = Path.Combine(gameFolder, "Icon.png");        // square icon, used in lists/taskbar

        bool needCapsule = !File.Exists(capsulePath);
        bool needHero = !File.Exists(heroPath);
        bool needIcon = !File.Exists(iconPath);

        if (needCapsule || needHero || needIcon)
        {
            int? gameId = null;
            try
            {
                gameId = sgdb.SearchGameId(gameName);
            }
            catch (Exception ex)
            {
                logger.Warn($"GameDrive: SteamGridDB search failed for '{gameName}': {ex.Message}");
            }

            if (gameId == null)
            {
                logger.Warn($"GameDrive: no SteamGridDB match for '{gameName}', skipping media download.");
            }
            else
            {
                if (needCapsule)
                {
                    TryDownload(sgdb, "grids", gameId.Value, "600x900", capsulePath, logger, "capsule");
                }
                if (needHero)
                {
                    TryDownload(sgdb, "heroes", gameId.Value, "1920x620", heroPath, logger, "hero/background");
                }
                if (needIcon)
                {
                    TryDownload(sgdb, "icons", gameId.Value, null, iconPath, logger, "icon");
                }
            }
        }

        if (File.Exists(capsulePath)) metadata.CoverImage = new MetadataFile(capsulePath);
        if (File.Exists(heroPath)) metadata.BackgroundImage = new MetadataFile(heroPath);
        if (File.Exists(iconPath)) metadata.Icon = new MetadataFile(iconPath);
    }

    private void TryDownload(SteamGridDbClient sgdb, string category, int gameId, string dimensions, string destPath, ILogger logger, string label)
    {
        try
        {
            string url = sgdb.GetImageUrl(category, gameId, dimensions);
            if (url == null)
            {
                logger.Warn($"GameDrive: no {label} found on SteamGridDB for game id {gameId}.");
                return;
            }

            sgdb.DownloadFile(url, destPath);
            logger.Info($"GameDrive: downloaded {label} -> {destPath}");
        }
        catch (Exception ex)
        {
            logger.Warn($"GameDrive: failed to download {label} for game id {gameId}: {ex.Message}");
        }
    }
}

// Minimal SteamGridDB REST client (https://www.steamgriddb.com/api/v2).
// Only uses what's needed here: search, grids, heroes, icons.
public class SteamGridDbClient
{
    private static readonly HttpClient http = new HttpClient();
    private readonly string apiKey;
    private readonly ILogger logger;

    public SteamGridDbClient(string apiKey, ILogger logger)
    {
        this.apiKey = apiKey;
        this.logger = logger;
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

        if (result?.Data != null && result.Data.Count > 0)
        {
            return result.Data[0].Id;
        }

        return null;
    }

    // category: "grids", "heroes", or "icons". dimensions is optional (e.g. "600x900").
    public string GetImageUrl(string category, int gameId, string dimensions)
    {
        string url = $"https://www.steamgriddb.com/api/v2/{category}/game/{gameId}?mimes=image/png";
        if (!string.IsNullOrEmpty(dimensions))
        {
            url += "&dimensions=" + dimensions;
        }

        var result = SendAndParse<SgdbListResponse<SgdbImage>>(url);

        if (result?.Data != null && result.Data.Count > 0)
        {
            return result.Data[0].Url;
        }

        return null;
    }

    public void DownloadFile(string url, string destPath)
    {
        using (var response = http.GetAsync(url).GetAwaiter().GetResult())
        {
            response.EnsureSuccessStatusCode();
            var bytes = response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult();
            File.WriteAllBytes(destPath, bytes);
        }
    }
}

[DataContract]
public class SgdbListResponse<T>
{
    [DataMember(Name = "success")]
    public bool Success { get; set; }

    [DataMember(Name = "data")]
    public List<T> Data { get; set; }
}

[DataContract]
public class SgdbGame
{
    [DataMember(Name = "id")]
    public int Id { get; set; }

    [DataMember(Name = "name")]
    public string Name { get; set; }
}

[DataContract]
public class SgdbImage
{
    [DataMember(Name = "id")]
    public int Id { get; set; }

    [DataMember(Name = "url")]
    public string Url { get; set; }
}