Feature: Enrolling the owner's voice
  The owner records a few clips of their own speech on each microphone they
  use, and `ears enroll` turns them into a private voiceprint for that device.
  Nothing is recorded by the command itself; it reads the files it is given.

  Scenario: Enrolling from files creates a private voiceprint for that device
    Given no voiceprint exists
    When the owner enrolls "Logitech BRIO" from 3 clips of their speech
    Then a voiceprint exists for "Logitech BRIO" made from 3 clips
    And only the owner can read the voiceprint file
    And the command does not print the voiceprint itself

  Scenario: A second microphone gets its own voiceprint and keeps the first
    Given the owner has enrolled "Logitech BRIO"
    When the owner enrolls "AirPods" from 3 clips of their speech
    Then a voiceprint exists for "Logitech BRIO" made from 3 clips
    And a voiceprint exists for "AirPods" made from 3 clips

  Scenario: Clips with no usable speech are left out and named
    Given no voiceprint exists
    When the owner enrolls "AirPods" from 2 clips of their speech and 1 silent clip
    Then a voiceprint exists for "AirPods" made from 2 clips
    And the command names the silent clip as skipped

  Scenario: Nothing usable means no voiceprint
    Given no voiceprint exists
    When the owner enrolls "AirPods" from 0 clips of their speech and 2 silent clips
    Then the command fails
    And no voiceprint exists

  Scenario: A voiceprint made with another model is not mixed with a new one
    Given the owner has enrolled "Logitech BRIO" with another model
    When the owner enrolls "AirPods" from 3 clips of their speech
    Then the command fails and says to re-enroll
    And the voiceprint for "Logitech BRIO" is unchanged

  Scenario: Deleting removes the voiceprint
    Given the owner has enrolled "Logitech BRIO"
    When the owner deletes their voiceprint
    Then no voiceprint exists
