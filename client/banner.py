banner = r"""
██████╗ ███████╗██╗  ████████╗ █████╗ ██████╗ ██╗      █████╗ ██╗   ██╗
██╔══██╗██╔════╝██║  ╚══██╔══╝██╔══██╗██╔══██╗██║     ██╔══██╗╚██╗ ██╔╝
██║  ██║█████╗  ██║     ██║   ███████║██████╔╝██║     ███████║ ╚████╔╝
██║  ██║██╔══╝  ██║     ██║   ██╔══██║██╔═══╝ ██║     ██╔══██║  ╚██╔╝
██████╔╝███████╗███████╗██║   ██║  ██║██║     ███████╗██║  ██║   ██║
╚═════╝ ╚══════╝╚══════╝╚═╝   ╚═╝  ╚═╝╚═╝     ╚══════╝╚═╝  ╚═╝   ╚═╝

                Music Streaming Platform for SysADs

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Stream music over secure TCP/TLS connections
  JWT Authentication & Session Management
  Adaptive Bitrate Audio Streaming
  FFmpeg Powered Audio Processing
  Songs playable using Youtube links

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
print(banner)

HELP_TEXT = """
╔════════════════════════════════════════════════════════════════════════════╗
║                           DELTAPLAY COMMANDS                               ║
╚════════════════════════════════════════════════════════════════════════════╝

🕹️ MUSIC CONTROL

  list
      Show currently loaded songs.

  library
      Refresh and display the complete music library.

  play <index>
      Play the selected song.

  pause
      Pause current playback.

  resume
      Resume playback.

  stop
      Stop playback.

  status
      Display:
        • Current Track
        • Network RTT
        • Buffer Health
        • Connection Status

______________________________________________________________________________

📂 PLAYLISTS


  playlists
      Show all playlists.

  create_playlist <name> [idx,idx,...]
      Create a playlist and optionally add songs.

      Example:
          create_playlist Chill 1,3,5

  load_playlist <index>
      Load playlist songs into current queue.

  delete_playlist <index>
      Delete a playlist.

  add_to_playlist <playlist_index> <song_index>
      Add a song to a playlist.

      Example:
          add_to_playlist 2 4

______________________________________________________________________________

⬇️ DOWNLOADS

  download <links.txt>
      Read YouTube URLs from a text file and download
      them on the DeltaPlay server.

      Example:
          download songs.txt

______________________________________________________________________________
          
🕘 HISTORY

  history
      Show previously played tracks.

______________________________________________________________________________
      
⚙️ SYSTEM 

  help
      Show this menu.

  quit
      Disconnect and exit DeltaPlay.

______________________________________________________________________________

"""