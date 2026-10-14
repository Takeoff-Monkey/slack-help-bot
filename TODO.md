1. setup a DB for storing personalized user data the AI can access as needed
2. Arazoza worksheet formatter:
    - the last column at the end called 'column1' needs to be filled w/ scraped data from the 'QTY' column on the attached schedule
        - when the user sends in an Arazoza job request, there should be a schedule (or schedules) along with the worksheet file you're modifying
        - these schedules will be images, the names corresponding to 
3. ~~schedule-extractor widget is relying on schedule headers in images~~ → `tools/schedule-extractor/main2.py` (2026-10-14)
    - ~~images are most often screenshots, so it shouldn't look for headers~~
4. wait for warm load
    - the bot impatiently tries to use the sandbox before it's ready; if it's not ready right when it goes to use it, it needs to back off for 15-20 seconds and then try again
    - it should give a max of 3 tries before giving up
5. needs to be able to see all documents in the chat
    - currently if the user asks a follow-up on the bot's output file, or if the user posts a revised file/screenshot, the bot trips up
6. ~~the bot needs to be less talkative with its responses overall~~ → `status.py` + `agent_loop.py` (2026-10-14)
    - ~~it needs to post fewer status updates~~
        - ~~if it's gone 30 seconds without an update, it should post something concise like "Still working, about halfway there..." or "Almost done, just a few more steps...", depending on what's appropriate~~
        - ~~it shouldn't post an update for every new thought it has or every little issue it runs into~~
    - ~~when it runs into errors, it needs to reframe them as more positive~~
        - ~~the average user sees "I ran into an error doing X, so I need to try a different approach" as "I can't do what you want", which will scare them off and make them think the bot is broken~~
        - ~~instead, it should say something like "X is taking a little longer, but I'm still making progress" or "Ran into a minor issue with X, so I'm trying Y instead"~~
        - ~~always keep it light, casual, worry-free, and optimistic/positive~~
        - ~~if it truly can't proceed or runs into an error, it should apologetically say so and ask the user what they'd like to do or if they want to try again in a new chat~~
            - ~~it should always give the user an option(s) for how to resolve the issue~~
    - ~~it should be more concise in its regular responses, and less conversational~~
        - ~~instead of "I've got it! Here's the file for the extracted schedule you asked for" it should just say "Here's the file"~~
7. **message icons**
    - these will help the user be able to see status as quickly as possible, even before reading the message
    - every message during it's thought process should be prefaced with an emoji, like maybe a gear or thinking icon
    - its success output message should use a success emoji, like a green checkbox or the tada icon
    - needing feedback should preface with a question mark or warning icon or similar to indicate it needs something from the user
    - a true failure/emergency output message should use a failure emoji, like a red x