This is your backlog. When you do an item, mark it as done (do not remove). Do all of the open items below. Keep iterating until done. Do not ask for permission to continue, you have it. Just keep going, even if it's a lot. Group changes in PRs. Once done, merge all PRs and publish a new minor release.

[ ] Rewrite the front page text to more accurately capture the system as it is. Focus first on reading existing dispatches, then on the option to generate your own.
[x] Move podcast settings also to the Dispatch settings page.
[x] Add an Mark all as Read button to the Editions page.
[x] For all users, do not show the "published" badge when viewing a dispatch of another user.
[ ] Automatically create the PDF export for all editions. Make sure it shows in the edition display (the icon) everywhere we should show it.
[x] Change the button "Email me" to "Subscribe to emails". Keep the icon. When active, it should be "Subscribed to emails" with a checkmark instead of the email icon.
[x] Remove the header description text from Dispatches and Editions.
[x] On the Dispatch details page, under Average cost for generation, add a note that only the publisher of the dispatch pays these costs.
[x] On the Dispatch details page, change "Copy to my Dispatch" to "Copy configuration to my Dispatch".
[x] Make the featured edition on the homepage *always* be the most recent edition of the AI Tech Dispatch by rjbruin. Make the featured dispatch (not the edition but the dispatch) configurable on the admin panel.
[ ] Let's implement podcast setup for other dispatches. For this we need to refactor the Elevenlabs setup slightly: instead of a global API key, have one per user that has their own dispatch. They can set it like they set the OpenRouter key. Include instructions for setting up an account and key with Elevenlabs. Only the owner of a dispatch can either turn on automatic podcast generation, which uses their own API key, or click the button to generate per individual edition. Podcasts for a dispatch will become available to all subscribed users. Show a button Subscribe to Podcast on the page for the edition, as well as on the Dispatch card and on the dispatch details page. This button should always open a modal with instructions for to subscribe to the podcast in a podcast app using the RSS link, of course also giving the link.