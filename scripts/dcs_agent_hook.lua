--[[
  dcs_agent_hook.lua — DCS Agent status hook
  Install to: Saved Games/{server}/Scripts/Hooks/dcs_agent_hook.lua

  Writes a JSON file to Saved Games/{server}/Logs/dcs_agent_status.json
  whenever mission state or player list changes.
  The DCS Agent reads this file to populate runtime status.
--]]

local UPDATE_INTERVAL = 5 -- seconds between periodic refreshes

local _last_update = 0
local _mission_loaded = false

-- Player cache: id -> name  (maintained via callbacks, bypasses net.getAllPlayers)
local _players = {}

local _status = {
	mission_name = "",
	map = "",
	player_count = 0,
	players = {},
	mission_loaded = false,
	mission_time_seconds = 0,
}

-- ── JSON helpers (no external deps) ─────────────────────────────────────────

local function esc(s)
	return tostring(s or ""):gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "")
end

local function jstr(s)
	return '"' .. esc(s) .. '"'
end

local function iso_now()
	local t = os.date("!*t")
	return string.format("%04d-%02d-%02dT%02d:%02d:%02dZ", t.year, t.month, t.day, t.hour, t.min, t.sec)
end

-- ── File write ───────────────────────────────────────────────────────────────

local function write_status()
	local path = lfs.writedir() .. "Logs\\dcs_agent_status.json"
	local f = io.open(path, "w")
	if not f then
		return
	end

	local player_parts = {}
	for _, name in ipairs(_status.players) do
		player_parts[#player_parts + 1] = jstr(name)
	end

	f:write(
		string.format(
			'{"mission_name":%s,"map":%s,"player_count":%d,"players":[%s],"mission_loaded":%s,"mission_time_seconds":%d,"updated_at":%s}',
			jstr(_status.mission_name),
			jstr(_status.map),
			_status.player_count,
			table.concat(player_parts, ","),
			_status.mission_loaded and "true" or "false",
			_status.mission_time_seconds,
			jstr(iso_now())
		)
	)
	f:close()
end

-- ── Player cache helpers ──────────────────────────────────────────────────────

local function _flush_players()
	-- Rebuild status.players from cache
	local list = {}
	for _, name in pairs(_players) do
		list[#list + 1] = name
	end
	_status.players = list
	_status.player_count = #list
end

local function _add_player(id)
	local ok, name = pcall(net.get_player_info, id, "name")
	if ok and type(name) == "string" and name ~= "" then
		_players[id] = name
	else
		_players[id] = "Player"
	end
	_flush_players()
end

local function _remove_player(id)
	_players[id] = nil
	_flush_players()
end

-- ── Mission data refresh ──────────────────────────────────────────────────────

local function refresh_mission()
	local ok, name = pcall(DCS.getMissionName)
	if ok and name and name ~= "" then
		_status.mission_name = name
	end
	-- Theatre / map name
	local ok2, theatre = pcall(function()
		return env.mission.theatre
	end)
	if ok2 and type(theatre) == "string" and theatre ~= "" then
		_status.map = theatre
	end
	-- Fallback: try env.mission.map
	if _status.map == "" then
		local ok3, map = pcall(function()
			return env.mission.map
		end)
		if ok3 and type(map) == "string" and map ~= "" then
			_status.map = map
		end
	end
end

-- ── Hook callbacks ───────────────────────────────────────────────────────────

local agentHook = {}

function agentHook.onMissionLoadBegin()
	_mission_loaded = false
	_status.mission_loaded = false
	_status.mission_name = ""
	_status.map = ""
	_status.players = {}
	_status.player_count = 0
	_status.mission_time_seconds = 0
	_players = {}
	write_status()
end

function agentHook.onMissionLoadEnd()
	_mission_loaded = true
	_status.mission_loaded = true
	refresh_mission()
	_flush_players()
	write_status()
end

function agentHook.onPlayerConnect(id)
	_add_player(id)
	write_status()
end

function agentHook.onPlayerDisconnect(id, _err)
	_remove_player(id)
	write_status()
end

function agentHook.onPlayerChangeSlot(id)
	-- Refresh name in case it changed on slot selection
	local ok, name = pcall(net.get_player_info, id, "name")
	if ok and type(name) == "string" and name ~= "" then
		_players[id] = name
		_flush_players()
	end
	write_status()
end

function agentHook.onSimulationFrame()
	if not _mission_loaded then
		return
	end
	local now = os.time()
	if now - _last_update >= UPDATE_INTERVAL then
		_last_update = now
		local ok, t = pcall(DCS.getModelTime)
		if ok then
			_status.mission_time_seconds = math.floor(t)
		end
		write_status()
	end
end

DCS.setUserCallbacks(agentHook)
