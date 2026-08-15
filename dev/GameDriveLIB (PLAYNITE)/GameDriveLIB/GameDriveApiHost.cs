using Playnite.SDK;
using Playnite.SDK.Models;
using Playnite.SDK.Plugins;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading;

/// <summary>
/// Keeps the GameDrive web service connected to Playnite's live database.
/// This is deliberately started from Playnite's application lifecycle instead
/// of the library-plugin constructor, so the API is available after Playnite
/// has finished initializing its extensions and database.
/// </summary>
public sealed class GameDriveApiHost : GenericPlugin
{
    private const string Prefix = "http://127.0.0.1:38123/";
    private static readonly Guid PluginId = Guid.Parse("5f5a9b1e-9b44-4f3c-9f5d-1d5e5a7e9a31");
    private readonly HttpListener server = new HttpListener();
    private readonly object serverLock = new object();
    private Thread serverThread;

    public override Guid Id => PluginId;

    public GameDriveApiHost(IPlayniteAPI api) : base(api)
    {
        // The actual listener is started in OnApplicationStarted. Playnite's
        // plugin constructors can run before the application/database is ready.
    }

    public override void OnApplicationStarted(Playnite.SDK.Events.OnApplicationStartedEventArgs args)
    {
        StartServer();
    }

    public override void OnApplicationStopped(Playnite.SDK.Events.OnApplicationStoppedEventArgs args)
    {
        StopServer();
    }

    public override void Dispose()
    {
        StopServer();
        base.Dispose();
    }

    private void StartServer()
    {
        lock (serverLock)
        {
            if (server.IsListening)
                return;

            try
            {
                server.Prefixes.Clear();
                server.Prefixes.Add(Prefix);
                server.Start();
                serverThread = new Thread(ServerLoop)
                {
                    IsBackground = true,
                    Name = "GameDrive Playnite API"
                };
                serverThread.Start();
                LogManager.GetLogger().Info($"GameDrive API host ready on {Prefix}");
            }
            catch (HttpListenerException ex)
            {
                // The legacy GameDriveLibrary host may already own the port.
                // Treat that as success rather than breaking the Playnite plugin.
                LogManager.GetLogger().Info($"GameDrive API host could not claim {Prefix}: {ex.Message}. Another GameDrive host may already be active.");
            }
            catch (Exception ex)
            {
                LogManager.GetLogger().Error(ex, "GameDrive API host failed to start.");
            }
        }
    }

    private void StopServer()
    {
        lock (serverLock)
        {
            try { server.Stop(); } catch { }
            try { server.Close(); } catch { }
            serverThread = null;
        }
    }

    private void ServerLoop()
    {
        while (server.IsListening)
        {
            try
            {
                var context = server.GetContext();
                ThreadPool.QueueUserWorkItem(_ => HandleRequest(context));
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
                LogManager.GetLogger().Warn($"GameDrive API listener error: {ex.Message}");
            }
        }
    }

    private void HandleRequest(HttpListenerContext context)
    {
        try
        {
            string path = (context.Request.Url.AbsolutePath ?? string.Empty).TrimEnd('/').ToLowerInvariant();

            if (path == "/health" && context.Request.HttpMethod == "GET")
            {
                WriteJson(context, new HealthResponse { Ok = true, Source = "PlayniteApi.Database.Games" });
                return;
            }

            if (path == "/games" && context.Request.HttpMethod == "GET")
            {
                // Read the live Playnite database. Do not read games.db/LiteDB
                // from disk: Playnite is the source of truth for installed state,
                // IDs, actions and install paths.
                var games = PlayniteApi.Database.Games
                    .Where(g => g != null && g.IsInstalled)
                    .Select(ToLibraryGame)
                    .ToList();

                WriteJson(context, games);
                return;
            }

            WriteJson(context, new ErrorResponse { Ok = false, Error = "not_found" }, 404);
        }
        catch (Exception ex)
        {
            LogManager.GetLogger().Error(ex, "GameDrive API request failed.");
            try
            {
                WriteJson(context, new ErrorResponse { Ok = false, Error = ex.Message }, 500);
            }
            catch { }
        }
    }

    private LibraryGame ToLibraryGame(Game game)
    {
        GameAction action = null;
        if (game.GameActions != null)
            action = game.GameActions.FirstOrDefault(a => a != null && a.IsPlayAction)
                ?? game.GameActions.FirstOrDefault();

        return new LibraryGame
        {
            Id = game.Id,
            Name = game.Name,
            GameId = game.GameId,
            SourceId = game.SourceId,
            IsInstalled = game.IsInstalled,
            InstallDirectory = game.InstallDirectory,
            Executable = action?.Path,
            Arguments = action?.Arguments,
            WorkingDirectory = action?.WorkingDir,
            Description = game.Description,
            ReleaseDate = game.ReleaseDate,
            Playtime = game.Playtime
        };
    }

    private static void WriteJson(HttpListenerContext context, object value, int statusCode = 200)
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

    [System.Runtime.Serialization.DataContract]
    private sealed class HealthResponse
    {
        [System.Runtime.Serialization.DataMember(Name = "ok")]
        public bool Ok { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "source")]
        public string Source { get; set; }
    }

    [System.Runtime.Serialization.DataContract]
    private sealed class ErrorResponse
    {
        [System.Runtime.Serialization.DataMember(Name = "ok")]
        public bool Ok { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "error")]
        public string Error { get; set; }
    }

    [System.Runtime.Serialization.DataContract]
    private sealed class LibraryGame
    {
        [System.Runtime.Serialization.DataMember(Name = "id")]
        public Guid Id { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "name")]
        public string Name { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "gameId")]
        public string GameId { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "sourceId")]
        public Guid SourceId { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "isInstalled")]
        public bool IsInstalled { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "installDirectory")]
        public string InstallDirectory { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "executable")]
        public string Executable { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "arguments")]
        public string Arguments { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "workingDirectory")]
        public string WorkingDirectory { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "description")]
        public string Description { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "releaseDate")]
        public DateTime? ReleaseDate { get; set; }
        [System.Runtime.Serialization.DataMember(Name = "playtime")]
        public long Playtime { get; set; }
    }
}
