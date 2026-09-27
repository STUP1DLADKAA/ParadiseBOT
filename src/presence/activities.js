import { ActivityType } from 'discord.js';

const playing = (name) => ({ name, type: ActivityType.Playing });
const watching = (name) => ({ name, type: ActivityType.Watching });
const listening = (name) => ({ name, type: ActivityType.Listening });
const competing = (name) => ({ name, type: ActivityType.Competing });
const custom = (state) => ({ name: 'Custom Status', state, type: ActivityType.Custom });

export const activities = Object.freeze({
  normal: [
    playing('Minecraft'),
    watching('the rain'),
    listening('music'),
    watching('a documentary'),
    playing('chess badly'),
    listening('the same song again'),
    watching('clouds'),
    custom('Thinking'),
    custom('Taking a minute'),
  ],
  gaming: [
    playing('one more round'),
    playing('something offline'),
    playing('with bad settings'),
    playing('on peaceful mode'),
    watching('someone else play'),
    custom('Looking for a save point'),
    custom('Lost in the settings menu'),
    custom('Probably AFK'),
    competing('a very casual tournament'),
  ],
  weird: [
    watching('the corner of the room'),
    listening('the fridge hum'),
    playing('with 3 braincells'),
    custom('Counting pixels'),
    custom('Looking for snacks'),
    custom('I think the wall moved'),
    custom('Checking under the rug'),
    custom('Standing slightly to the left'),
    custom('Trying to remember a word'),
  ],
  sleepy: [
    custom('Probably sleeping'),
    custom('Barely awake'),
    custom('Considering a nap'),
    custom('Five more minutes'),
    listening('the fan'),
    watching('the ceiling'),
    playing('with my eyes closed'),
    custom('Running on low battery'),
    custom('Too early for this'),
  ],
  chaotic: [
    custom('Losing my mind'),
    custom('Everything is under control'),
    custom('Making it worse'),
    custom('No time to explain'),
    watching('things unfold'),
    listening('several noises at once'),
    playing('the wrong game'),
    competing('nobody in particular'),
    custom('This was not the plan'),
  ],
  mysterious: [
    custom('Investigating'),
    custom('Doing important research'),
    custom('Questioning everything'),
    watching('from a safe distance'),
    listening('for something'),
    custom('Noticing things'),
    custom('Following a lead'),
    custom('Checking something'),
    custom('I know enough'),
  ],
  server: [
    watching('the server'),
    watching('everyone'),
    custom('Walking around Paradise'),
    custom('Waiting for someone to talk'),
    custom('Keeping an eye on things'),
    listening('Paradise'),
    custom('Still here'),
    custom('Reading the room'),
    custom('Checking who is awake'),
  ],
  pointless: [
    custom('Existing'),
    custom('Doing absolutely nothing'),
    custom('Touching grass'),
    watching('paint dry'),
    listening('nothing'),
    custom('Avoiding responsibilities'),
    custom('Charging...'),
    custom('Moving a chair'),
    custom('Waiting for no reason'),
  ],
});

export const activityCategories = Object.freeze([
  { name: 'normal', weight: 22 },
  { name: 'gaming', weight: 13 },
  { name: 'weird', weight: 13 },
  { name: 'sleepy', weight: 10 },
  { name: 'chaotic', weight: 7 },
  { name: 'mysterious', weight: 10 },
  { name: 'server', weight: 14 },
  { name: 'pointless', weight: 11 },
]);
