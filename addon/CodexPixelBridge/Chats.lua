-- Persistent in-game chat list, drafts, transcripts and bridge settings.
local ADDON_NAME, NS = ...
local panel, edit = NS.Panel, NS.PromptEditBox
local chats, order, pending = {}, {}, {}
local rows, offset, ready = {}, 0, false
local render, refresh
local function literal(value) return (tostring(value):gsub('|','||')) end
local function validID(value) return type(value)=='string' and #value<=32 and value:match('^[a-z0-9][a-z0-9_-]*$') end
local function ensure(id)
    if not validID(id) then return end
    if not chats[id] then
        if #order>=16 then NS.SetStatus('Maximum 16 chats. Archive a chat first.');return end
        chats[id]={id=id,title=id,messages={},draft='',state='idle'}
        order[#order+1]=id
    end
    return chats[id]
end
NS.CanSelectChat=function(id) return ensure(id)~=nil end
local function save()
    if not ready then return end
    CodexPixelBridgeState.chatsV2={chats=chats,order=order,pending=pending}
end
local sidebar=CreateFrame('Frame',nil,panel,'BackdropTemplate')
sidebar:SetSize(202,740);sidebar:SetPoint('TOPRIGHT',panel,'TOPLEFT',-4,0)
if sidebar.SetBackdrop then
    sidebar:SetBackdrop({bgFile='Interface\\Buttons\\WHITE8x8',edgeFile='Interface\\Buttons\\WHITE8x8',edgeSize=1})
    sidebar:SetBackdropColor(.055,.065,.085,.97);sidebar:SetBackdropBorderColor(.25,.3,.4,1)
end
local label=sidebar:CreateFontString(nil,'OVERLAY','GameFontNormalLarge')
label:SetPoint('TOPLEFT',12,-16);label:SetText('Chats')
local connection=sidebar:CreateFontString(nil,'OVERLAY','GameFontHighlightSmall')
connection:SetPoint('TOPLEFT',12,-43);connection:SetWidth(178);connection:SetText('Bridge: awaiting connection')
NS.ChatConnection=function(text) connection:SetText(text) end
local function button(parent,text,x,y,width,action)
    local b=CreateFrame('Button',nil,parent,'UIPanelButtonTemplate')
    b:SetSize(width,24);b:SetPoint('TOPLEFT',x,y);b:SetText(text);b:SetScript('OnClick',action)
    return b
end
button(sidebar,'Connect / Sync',12,-65,178,function()
    if NS.UseSlotReturn then NS.UseSlotReturn() end
    NS.SendChatOperation('sync')
end)
-- One reusable inline editor; no desktop interaction is needed for chat setup.
local dialog=CreateFrame('Frame',nil,panel,'BasicFrameTemplateWithInset')
dialog:SetSize(440,138);dialog:SetPoint('CENTER');dialog:SetFrameStrata('DIALOG');dialog:Hide()
local input=CreateFrame('EditBox',nil,dialog,'InputBoxTemplate')
input:SetSize(394,26);input:SetPoint('TOPLEFT',22,-44);input:SetAutoFocus(false);input:SetMaxBytes(1000)
local apply
local function accept()
    if apply and apply(input:GetText())~=false then dialog:Hide();input:ClearFocus() end
end
button(dialog,'Save',238,-91,80,accept)
button(dialog,'Cancel',328,-91,90,function() dialog:Hide();input:ClearFocus() end)
input:SetScript('OnEnterPressed',accept)
input:SetScript('OnEscapePressed',function() dialog:Hide();input:ClearFocus() end)
local function ask(title,value,callback)
    dialog.TitleText:SetText(title);input:SetText(value or '');apply=callback;dialog:Show();input:SetFocus()
end
local function operation(kind,value) return NS.SendChatOperation(kind,value) end
local function newChat()
    ask('New chat name (letters, numbers, - or _)', '', function(value)
        value=value:lower()
        if not validID(value) or not ensure(value) then NS.SetStatus('Invalid or unavailable chat name.');return false end
        NS.SetActiveChat(value);return operation('new','')
    end)
end
button(sidebar,'+ New chat',12,-97,178,newChat)
for i=1,12 do
    local row=button(sidebar,'',12,-133-(i-1)*33,178,function()
        local id=order[offset+i]
        if id then NS.SetActiveChat(id);NS.SendChatOperation('sync') end
    end)
    rows[i]=row
end
sidebar:EnableMouseWheel(true)
sidebar:SetScript('OnMouseWheel',function(_,delta)
    offset=math.max(0,math.min(math.max(0,#order-12),offset-delta));refresh()
end)
button(sidebar,'Rename',12,-547,84,function()
    local c=ensure(NS.GetActiveChat());ask('Rename chat',c.title,function(value) return operation('rename',value) end)
end)
button(sidebar,'Folder',106,-547,84,function()
    local c=ensure(NS.GetActiveChat());ask('Project folder',c.project or '',function(value) return operation('folder',value) end)
end)
button(sidebar,'Agent',12,-579,84,function()
    local c=ensure(NS.GetActiveChat());ask('Agent: codex or mock',c.backend or 'codex',function(value) return operation('backend',value:lower()) end)
end)
button(sidebar,'Reset',106,-579,84,function()
    ask('Reset agent memory? Type RESET','',function(value)
        if value~='RESET' then return false end
        return operation('reset','')
    end)
end)
button(sidebar,'Archive chat',12,-611,178,function()
    ask('Archive chat? Type ARCHIVE','',function(value)
        if value~='ARCHIVE' then return false end
        return operation('archive','')
    end)
end)
local details=sidebar:CreateFontString(nil,'OVERLAY','GameFontHighlightSmall')
details:SetPoint('TOPLEFT',12,-650);details:SetWidth(178);details:SetJustifyH('LEFT')
refresh=function()
    local selected=NS.GetActiveChat()
    for i,row in ipairs(rows) do
        local id=order[offset+i]
        row:SetShown(id~=nil)
        if id then
            local c=chats[id]
            row:SetText((id==selected and '> ' or '')..(c.unread and '* ' or '')..literal(c.title)..' ['..c.state..']')
        end
    end
    local c=ensure(selected)
    if c then details:SetText(literal(c.backend or 'codex')..' | '..literal(c.sandbox or 'read-only')..'\n'..literal(c.project or 'Default project')) end
end
render=function()
    local c=ensure(NS.GetActiveChat());if not c then return end
    c.unread=false
    local text={}
    for _,message in ipairs(c.messages) do
        text[#text+1]='You: '..message.prompt
        text[#text+1]='Codex ['..message.state..']: '..(message.text~='' and message.text or 'Waiting for bridge...')
    end
    if c.notice then text[#text+1]='Bridge: '..c.notice end
    NS.DisplayNativeReply(#text>0 and table.concat(text,'\n\n') or 'New conversation. Type a message below.')
    refresh();save()
end
NS.OnChatSelected=function(id,previous)
    local c=ensure(id);if not c then return end
    if chats[previous] then chats[previous].draft=edit:GetText() end
    edit:SetText(c.draft or '')
    render()
end
NS.RecordChatRequest=function(id,chat,text,op)
    local c=ensure(chat);if not c then return end
    pending[id]={chat=chat,operation=op}
    c.state='queued'
    if op=='send' then
        c.messages[#c.messages+1]={id=id,prompt=text,text='',state='queued'}
        if #c.messages>8 then table.remove(c.messages,1) end
        c.draft=''
    end
    connection:SetText('Bridge: waiting for receipt')
    render()
end
local function removeChat(id)
    chats[id]=nil
    for i=#order,1,-1 do if order[i]==id then table.remove(order,i) end end
    if NS.GetActiveChat()==id then NS.SetActiveChat('default') end
end
NS.ReceiveChats=function(bundle)
    if type(bundle)~='table' or type(bundle.rows)~='table' or #bundle.rows>176 then return nil end
    local parts,total={},0
    for _,row in ipairs(bundle.rows) do
        if type(row)~='table' or #row~=6 then return nil end
        for _,value in ipairs(row) do
            if type(value)~='string' or #value>60000 then return nil end
            total=total+#value;if total>600000 then return nil end
            parts[#parts+1]=#value..':'..value
        end
        if row[1]=='profile' or row[1]=='message' then
            if not validID(row[2]) then return nil end
        elseif row[1]~='receipt' then return nil end
    end
    local raw=table.concat(parts)
    local checksum=NS.Adler(raw)
    local n=0;for i=1,4 do n=n*256+checksum:byte(i) end
    if n~=bundle.checksum then return nil end
    local incoming={}
    for _,row in ipairs(bundle.rows) do
        local kind,id=row[1],row[2]
        if kind=='profile' then
            local c=ensure(id)
            if c then c.title=row[3];c.project=row[4];c.backend=row[5];c.sandbox=row[6];incoming[id]={} end
        elseif kind=='message' and incoming[id] then
            incoming[id][#incoming[id]+1]={id=row[3],state=row[4],prompt=row[5],text=row[6]}
        elseif kind=='receipt' then
            NS.AcknowledgePrompt(id)
            local p=pending[id]
            if p and (row[3]=='done' or row[3]=='failed' or row[3]=='interrupted') then
                if row[3]=='done' and p.operation=='archive' then removeChat(p.chat) end
                if row[3]=='failed' then
                    local c=chats[p.chat];if c then c.notice=row[4] end
                    NS.SetStatus(literal(row[4]))
                elseif chats[p.chat] and p.operation~='send' then chats[p.chat].notice=nil end
                pending[id]=nil
            end
        end
    end
    local working=false
    for id,messages in pairs(incoming) do
        local c=chats[id]
        if c then
            local seen={};for _,m in ipairs(messages) do seen[m.id]=true end
            for _,m in ipairs(c.messages) do if pending[m.id] and not seen[m.id] then messages[#messages+1]=m end end
            local last=messages[#messages]
            local old=c.messages[#c.messages]
            if last and (not old or old.text~=last.text or old.state~=last.state) and id~=NS.GetActiveChat() then c.unread=true end
            c.messages=messages;c.state=last and last.state or 'idle'
            for _,m in ipairs(messages) do if m.state=='queued' or m.state=='working' or m.state=='streaming' then working=true end end
        end
    end
    connection:SetText('Bridge: connected')
    render()
    return working or next(pending)~=nil
end
NS.ChatCommand=function(arg)
    if arg=='connect' or arg=='sync' then
        if NS.UseSlotReturn then NS.UseSlotReturn() end
        operation('sync','');return true
    end
    if arg=='new' then newChat();return true end
    local op,value=arg:match('^(%S+)%s+(.+)$')
    if op=='new' and validID(value) and ensure(value) then NS.SetActiveChat(value);operation('new','');return true end
    if op=='cd' then operation('folder',value);return true end
    if op=='agent' then operation('backend',value);return true end
    if op=='rename' then operation('rename',value);return true end
end
local events=CreateFrame('Frame')
events:RegisterEvent('ADDON_LOADED');events:RegisterEvent('PLAYER_LOGOUT')
events:SetScript('OnEvent',function(_,event,name)
    if event=='PLAYER_LOGOUT' then
        local c=ensure(NS.GetActiveChat());if c then c.draft=edit:GetText() end;save();return
    end
    if name~=ADDON_NAME then return end
    CodexPixelBridgeState=CodexPixelBridgeState or {}
    local saved=CodexPixelBridgeState.chatsV2
    ensure('default')
    if type(saved)=='table' and type(saved.chats)=='table' and type(saved.order)=='table' then
        for _,id in ipairs(saved.order) do
            local old=saved.chats[id];local c=ensure(id)
            if c and type(old)=='table' and type(old.messages)=='table' then
                c.title=type(old.title)=='string' and old.title or id;c.draft=type(old.draft)=='string' and old.draft or ''
                for _,m in ipairs(old.messages) do
                    if #c.messages<8 and type(m)=='table' and type(m.id)=='string' and type(m.prompt)=='string' and type(m.text)=='string' and type(m.state)=='string' then c.messages[#c.messages+1]=m end
                end
            end
        end
    end
    ensure('default');ensure(NS.GetActiveChat());ready=true;render()
    edit:SetText(chats[NS.GetActiveChat()].draft or '')
    if C_Timer and C_Timer.After then C_Timer.After(2,function() operation('sync','') end) end
end)
NS.ChatState=function() return chats,pending end
