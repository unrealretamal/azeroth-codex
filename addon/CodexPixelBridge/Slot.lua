-- Load-on-demand addon-slot reply transport.  This is the default return path:
-- the companion writes plain data into an existing unused addon, then WoW loads
-- that addon through documented APIs.  No font metrics, process access or input.
local ADDON_NAME, NS = ...
local SIZE, INTERVAL = 200, 5
local PREFIX = 'CodexPixelBridgeSlot'
local initialized = false
local slot, loaded, request = 1, 0, 0
local active, deadline, watchUntil = false, 0, 0
local advertised = false
local session = nil
local failures = 0
local fallback
local panel = NS.Panel
panel.TitleText:SetText('Azeroth Codex | Checked reply slots')

-- Native.lua creates this behind the panel before this module is loaded.
local font = {
    body = NS.NativeBody,
    display = NS.DisplayNativeReply,
    watching = NS.IsVisualWatching,
    pause = NS.PauseVisual,
    resume = NS.ResumeVisual,
    submitted = NS.OnPromptSubmitted,
    page = NS.ChangeVisualPage,
    control = NS.VisualControl,
}
if NS.NativeBody then NS.NativeBody:Hide() end
local body = CreateFrame('ScrollingMessageFrame', nil, panel)
body:SetPoint('TOPLEFT', 16, -118); body:SetSize(515, 512)
body:SetFontObject(GameFontHighlight); body:SetJustifyH('LEFT'); body:SetFading(false); body:SetMaxLines(5000)
body:EnableMouseWheel(true)
body:SetScript('OnMouseWheel', function(self, delta) if delta > 0 then self:ScrollUp() else self:ScrollDown() end end)
body:AddMessage('Send a prompt. Replies arrive through checked addon slots.')
NS.NativeBody = body
NS.DisplayNativeReply = function(text)
    NS.LastNativeReply = text
    body:Clear(); body:AddMessage((text:gsub('|', '||'))); body:ScrollToTop()
end

local badge = NS.Minimap:CreateFontString(nil, 'OVERLAY', 'GameFontNormalLarge')
badge:SetPoint('TOPRIGHT', NS.Minimap, 'TOPRIGHT', 8, 8); badge:SetText('')
panel:HookScript('OnShow', function() badge:SetText('') end)

local function slotName(number) return PREFIX .. string.format('%03d', number) end
local function addonLoaded(name)
    if C_AddOns and type(C_AddOns.IsAddOnLoaded) == 'function' then return C_AddOns.IsAddOnLoaded(name) end
    if type(IsAddOnLoaded) == 'function' then return IsAddOnLoaded(name) end
    return false
end
local function loadAddon(name)
    if C_AddOns and type(C_AddOns.LoadAddOn) == 'function' then return C_AddOns.LoadAddOn(name) end
    if type(LoadAddOn) == 'function' then return LoadAddOn(name) end
    return nil, 'LoadAddOn unavailable'
end
local function validState(state)
    return state == 'waiting' or state == 'queued' or state == 'working' or state == 'streaming'
        or state == 'done' or state == 'failed' or state == 'interrupted'
end
local function adler(text)
    local a, b = 1, 0
    for i = 1, #text do a = (a + text:byte(i)) % 65521; b = (b + a) % 65521 end
    return b * 65536 + a
end
local function watch()
    if not initialized then NS.SetStatus('Waiting for addon settings to load.'); return end
    if not active then
        local saved = tonumber(CodexPixelBridgeState.nextAddonSlotV1)
        if saved and saved >= 1 and saved <= SIZE + 1 and saved == math.floor(saved) then
            slot = math.max(slot, saved); loaded = slot - 1
        end
    end
    if slot > SIZE then
        if fallback and fallback('Addon-slot channel reached its session limit.') then return end
        NS.SetStatus('Addon-slot channel reached its session limit. Replies remain in the companion.'); return
    end
    if not active then deadline = GetTime() + 6 end
    active = true; watchUntil = GetTime() + 1200
    if NS.SetWatchState then NS.SetWatchState(true) end
end
local function advance(wait)
    loaded = slot; slot = slot + 1; deadline = GetTime() + (wait or INTERVAL)
    advertised = false
    CodexPixelBridgeState.nextAddonSlotV1 = math.max(tonumber(CodexPixelBridgeState.nextAddonSlotV1) or 1, slot)
end
local function pause()
    active = false
    if NS.SetWatchState then NS.SetWatchState(false) end
end
fallback = function(reason)
    if type(font.submitted) ~= 'function' or type(font.control) ~= 'function' then return false end
    pause()
    NS.ReturnMode = 'font'
    body:Hide()
    if font.body then font.body:Show(); NS.NativeBody = font.body end
    if font.display then NS.DisplayNativeReply = font.display end
    NS.IsVisualWatching = font.watching
    NS.PauseVisual = font.pause
    NS.ResumeVisual = font.resume
    NS.OnPromptSubmitted = font.submitted
    NS.ChangeVisualPage = font.page
    NS.VisualControl = font.control
    if request > 0 then font.submitted(request) end
    NS.SetStatus('Addon slots unavailable; using font fallback. '..tostring(reason))
    return true
end
NS.UseFontReturn = fallback
NS.ReturnMode = 'slot'
NS.IsVisualWatching = function() return active end
NS.PauseVisual = pause
NS.ResumeVisual = function() if request > 0 then failures = 0; watch() end end
NS.OnPromptSubmitted = function(sequence)
    -- Never let a newly submitted request inherit an unused slot that was
    -- already advertised (and may already contain frozen data) for another.
    if active or advertised then advance(6) end
    request = sequence; failures = 0; badge:SetText('')
    if NS.ClearReplyLinks then NS.ClearReplyLinks() end
    body:Clear(); body:AddMessage('Waiting for Codex...'); watch()
end
NS.ChangeVisualPage = function(delta) if delta < 0 then body:PageUp() else body:PageDown() end end

local function u16(number) return string.char(math.floor(number / 256) % 256, number % 256) end
NS.VisualControl = function(value)
    session = value
    if active then advertised = true end
    local remaining = active and math.max(0, math.min(6000, math.floor((deadline - GetTime()) * 1000))) or 0
    local data = NS.U32(slot)..NS.U32(remaining)..u16(1)..u16(active and 1 or 0)..NS.U32(request)..NS.U32(loaded)
    local body = 'CPBS'..string.char(3, #data, 0, 1)..session..NS.U32(0)..data..string.rep(string.char(0), 40 - #data)
    return body..NS.Adler(body)
end

local function finish(data, reason)
    advance(INTERVAL)
    if not data then
        failures = failures + 1
        NS.SetStatus('Retrying addon-slot reception: '..tostring(reason))
        if failures >= 3 then
            if not fallback('Repeated addon-slot errors: '..tostring(reason)) then
                pause()
                NS.SetStatus('Text reception paused after repeated errors: '..tostring(reason))
                body:AddMessage('The reply remains available in the companion. Resume preview to retry.')
            end
        end
        return
    end
    failures = 0
    local text, state = data.text, data.state
    NS.DisplayNativeReply(text)
    if state == 'done' or state == 'failed' or state == 'interrupted' then
        pause(); badge:SetText('!'); badge:SetTextColor(state == 'done' and .35 or 1, state == 'done' and 1 or .5, .55)
        NS.SetStatus(state == 'done' and 'Reply ready. Link idle; Send starts the next request.' or 'Request finished with a problem. Link idle; details below.')
    else
        NS.SetStatus(state == 'streaming' and 'Codex is writing...' or state == 'working' and 'Codex is working...' or 'Waiting for Codex...')
    end
end

local timer = CreateFrame('Frame', nil, UIParent)
timer:RegisterEvent('ADDON_LOADED')
timer:SetScript('OnEvent', function(self, _, name)
    if name ~= ADDON_NAME then return end
    CodexPixelBridgeState = CodexPixelBridgeState or {}
    slot = tonumber(CodexPixelBridgeState.nextAddonSlotV1) or 1
    if slot < 1 or slot > SIZE + 1 or slot ~= math.floor(slot) then slot = SIZE + 1 end
    loaded = slot - 1; initialized = true
end)
timer:SetScript('OnUpdate', function()
    if not initialized or not active or not session then return end
    local now = GetTime()
    if now >= watchUntil then pause(); NS.SetStatus('Text checks paused after 20 minutes. Resume to check again.'); return end
    if now < deadline then return end
    if slot > SIZE then
        if fallback and fallback('Addon-slot channel reached its session limit.') then return end
        pause(); NS.SetStatus('Addon-slot channel reached its session limit. Replies remain in the companion.'); return
    end
    CodexPixelBridgeState.nextAddonSlotV1 = math.max(tonumber(CodexPixelBridgeState.nextAddonSlotV1) or 1, slot + 1)
    local name = slotName(slot)
    if addonLoaded(name) then finish(nil, 'Slot already loaded') return end
    CodexPixelBridgeSlotData = nil
    local invoked, ok, reason = pcall(loadAddon, name)
    if not invoked or not ok then finish(nil, reason or ok) return end
    local data = CodexPixelBridgeSlotData
    local expected = ''
    for i = 1, #session do expected = expected .. string.format('%02x', session:byte(i)) end
    if type(data) ~= 'table' or data.version ~= 1 or data.session ~= expected or data.request ~= request
        or data.slot ~= slot or not validState(data.state) or type(data.text) ~= 'string'
        or #data.text > 60000 or type(data.revision) ~= 'number'
        or data.revision ~= adler(data.state..string.char(0)..data.text) then
        finish(nil, 'Invalid or stale slot data')
        return
    end
    finish(data)
end)
