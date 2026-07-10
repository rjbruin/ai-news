Do all of the below. Keep iterating until done. Do not ask for permission to continue, you have it. Just keep going, even if it's a lot. Group changes in PRs. Once done, merge all PRs and publish a new minor release.

* Either the preview server or production is sending a lot of emails to newbie@example.com to confirm account registration. Stop doing this, it's clouding the email box.
* Add some kind of progress feedback to the Admin panel when 1) polling all sources and when 2) polling an individual source. Possibly implement this per source and have polling all sources just show the progress in each individual source. Figure it out.
* In the Editions table, make the name of the edition clickable to open the edition.
* In the navigation, swap the places of News and Editions.
* Allow the user to add and manage email addresses that receive the edition emails. By default, have it be the email address of the account. Allow the user to add and remove other email addresses on the Settings page. When added, the user needs to confirm. Send a confirmation mail through the Newsletter inbox IMAP with a link to confirm adding the email address. When an email is removed, send a notification email. When the user removes all email addresses, uncheck the box for "Send as email newsletter...". When the user puts a first email address, check the box.
* Resurface the Tags page, and make it accessible to the admin in the navigation.
* In Settings, replace "agentic editions" with simply "editions".
* Move the Admin nav entry to the right side of the navigation, as the last entry before Sign Out.
* Show the generation cost of the edition as a separate box section at the bottom of the edition, above the Coverage box. Remove the badges just below the edition headline on the edition page. 
* Track the cost of podcast generation. Elevenlabs uses credits. 10k credits cost $2. Add these cost as a separate entry to the costs box on the edition page. In the Editions page, add the cost in the badge of the podcast icon.
* On the admin page, have a section Costs which uses multiple graphs to give insights about the cost of the global API keys, both OpenRouter and Elevenlabs. Figure out what is the best way to do this.