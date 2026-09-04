1. setup a DB for storing personalized user data the AI can access as needed
2. ~~instead of editing every time it does something new, this should be a new message for each new thought~~
    - ~~only the first real message should override the 'thinking...' message~~
3. ~~when using its sandbox, the bot needs to wait longer before trying to use it - currently it impatiently retries every ~1 second, which doesn't give it enough time to boot up~~
4. ~~the bot should truncate its messages so they're shorter and more human-readable~~
5. ~~the bot seems to sometimes silently drop off mid-task without trying to overcome its obstacle or even explaining why~~
    - ~~this leaves the user thinking it's still working when it's really not~~
    - ~~after 30 seconds of inactivity, it should update the user with where it's at: whether it's waiting for something, investigating something, has hit a snag, etc.~~
    - ~~when it truly reaches a wall or there's some error, it needs to immediately tell the user instead of silently failing~~
    - ~~it should also ask for clarification whenever it's about to take a risky action (e.g. deleting a file, making a permanent change, etc.)~~
6. ~~Build a tool for formatting Arazoza worksheets:~~ → `tools/arazoza-formatter/` (2026-09-04; Lambda built, not yet deployed)
    - ~~from the hardcoded list of takeoff groupings, match each item in 'Description' column to its logical group~~
        - ~~this hardcoded list is in tools/arazoza-formatter/takeoff-groupings.txt~~
        - ~~only perform this operation for cells in the 'Description' column that are black text (not other non-default colors)~~
        - ~~match as close as possible; if there isn't a good match, or there's more than one match, flag the entire cell by coloring it red~~
    - ~~move every row w/ data in 'Size' column to 'Description' column~~
        - ~~existing data in column B must stay, but it will always be on different rows than the rows that are moved over~~
    - ~~'Notes' column contains sizing info; move to 'Size' column~~
    - ~~The new 'Size' (taken from 'Notes') then needs to be copied and merged with the new 'Description'~~
        - ~~append, glue together w/ ' - '~~
    - ~~for soil and mulch items, if it says 'depth' anywhere in the item description, copy the value into the 'Size' column, and add 'Depth' to the 'Package' column~~
        - ~~do not copy over if there's just some generic size listed, but it doesn't explicitly say 'depth'~~
    - ~~convert the following UOM values (case-insensitive):~~
        - ~~'count', 'ea', 'each' -> 'Unit'~~
        - ~~'sf' -> 'Square Feet'~~
        - ~~'lf' -> 'Linear Feet'~~
    - ~~in 'Size', copy any of the following packages into the 'Packages' column:~~
        - ~~FG~~
        - ~~cont~~
        - ~~container~~
        - ~~b&b~~
        - ~~if 2 or more exist, they need to be listed w/ a '/' between (both listed in 'Package')~~
    - **DON'T DO NOW, LATER FEATURE**: ~~the last column at the end called 'column1' needs to be filled w/ scraped data from the 'QTY' column on the attached schedule~~
        - ~~when the user sends in an Arazoza job request, there should be a schedule (or schedules) along with the worksheet file you're modifying~~
        - ~~these schedules will be images, the names corresponding to ~~