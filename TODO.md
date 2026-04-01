Here are some thoughts about the overall organization of the project.

# Data

The format for data storage of game results is something like:

season,     age_agroup, date,         division,   geography,  home_team,      away_team,      home_goals, away_goals
Fall 2025,  Over 55,    20025-09-71,  2,          South,      Irish Village,  FC Westwood,    4,          0

This also suggests that we need other tables which help make sense of this  data. For example, there is a `seasons` table which lists the 60 or so seasons. There might be similar tables for `division` and `geography`. To track teams over time, we might have `team` table. There are many teams named "Irish Village," but they play in different divisions. I think that the `age_group` values have  changed over time. They split the old over-50 into over-48 and over-55, for example. But I think that they back-cast the new labels into the past. Maybe?

# Download data

We need several distinct scripts/functions which will be used in two different contexts. First, they are used when we download historical data. We only do this once, by hand, although we  maintain the ability to do it again, since we will probably do it before the start of each season. Second, the scripts/functions are used to download data for the current season. An example would be a script which takes a season as an argument and returns all the age_groups. Then another script can accept a season and age_group and return all the geographies. Then another script which takes a season, age_group and geography and returns all the flights (if that is the term). And so on.

# Download History

We might have a directory named `download_history` which includes the scripts  which are just used when we re-populate the historical data. An example of such a script is something which provides a list of all the seasons. (To be honest, we just might hard code the seasons into this function, adding the latest season each time we rebuild the historical data.)

# Download Current Season

We might have a directory named `download_current` which includes code for downloading and processing the current season. For the most part, this is just calling scripts which live elsehwere. Might it also needs to handle when it is run, probably automatically several times a week. Also, it needs to be more careful about annoying the OTHSL server. It might also try to handle issues of postponed games, card deductions and so on in a more robust fashion.

In any event, it at least produces a table which looks exactly like the historical table. It includes rows for all the games scheduled for the season, which missing values for goals for games which have not been played yet.

# Calculate ELO

The function which updates an ELO score requires two arguments: the current ELO and the result of the most recent game. Some perhaps `update_elo` is a good name. I suspect that we also need a function called `rolling_elo` which takes a dataset, probably formatted like that discussed above, and the calculates a team's ELO at each moment in time. 

Handling the start of a season is important. We know something about a team from it's results in the prior season. (I doubt that we care much about it's results from seasons before that other than, perhaps, what divisions it has been in over the last few seasons.) But we don't want to "anchor" to much on the prior season since there is so much player turnover between seasons. A team with a high ELO from the last few seasons, but which is blown out in its first two games (against teams which were blown out by other teams in their other game) is almost certainly a bad team.

# Simulation

Given ELO scores and the schedule, we can simulate the rest of the season. (In those simulations, a key question is whether we hold ELO fixed during the rest of the simulated season or allow it to change based on simulated outcomes. This is a subtle issue!) Once we simulate out the rest of the season 10,000 or so times, we can answer all sorts of questions. Some questions will be absolute: What is the probability of relegation? Some are conditional: What is the probability of relegation if we lose this week? Some are simple arithmetic based on these probabilities: How much does our probability of relegation increase if we lose this week.

Some questions are very tricky: What is the most important game to win over the next three weeks if our main goal is to avoid relegation?

Running simulations, storing the results and then using those results to create some interesting and interactive graphics should be fun!

# Homepage

There is a homepage which gives a sentence or two overview of the project and then shows the "standard" team page, perhaps defaulting to my team. It allows a user to choose a different team to look at, only using the latest data for this season. It will eventually allow a user to pick any team on any date in history. There will be some other pages to select from the menu, including a detailed description of the methodoloy. It should allow for cookies which will allow the page to default to the user's last selected team, which will probably be his own team.

# Team Default Page

The default page for a team will include its results, its future games and the current flight standings. This portion will be very similar to what OTHSL itself shows for a team. But it will also include some model-derived calculations, probably just for itself --- although it wll allow you to click on the name of any other team in the flight and go to their default page. Calculated items:

* Probabilities of which place in the flight --- first, fourth, etce --- you will end up in, with perhaps some summed probabilities of playoffs/promotions and relegation. 

* Time series of the three main probabilities (promotion, stay, relegation) over the course of the season till now).

* Key games in the future.

# Other Ideas

* Integrate with Ghost so that we have our own email list and newsletter. Eventually, we sent out an email each Wednesday with interesting items, like the most competitive flight, the highest leverage games this week-end, and so on.

* Have some more tabs which tell interesting stories. Which flight in history featured the biggest "surprises" over the course of the season?

# Schedule

We will not build the historical data perfectly before going on. Instead, we will create an MVP --- minimally viable product --- first, something which at least shows basic information about my team. And then iteratively improve it over time. We build the entire loop, from data download to website display, in the simplest possible fashio and then go back and make each part of the process more sophisticated. This suggests the follow steps:

* Download data for current season.

* Display data for Irish Village on homepage.

* Automate this to run each night at midnight.

That is not a bad MVP!
