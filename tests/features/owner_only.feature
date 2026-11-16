Feature: Owner-only listening
  With speaker mode "owner", only the owner's voice reaches the model.
  Anyone else's speech is never transcribed. Speech too short to verify is
  delivered only when it directly follows the owner, marked "?" as
  unverified, so a quick "yes" still gets through and a television's
  quick phrase does not.

  Background:
    Given the owner has a calibrated voiceprint for "AirPods"
    And the listener hears through "AirPods"
    And speaker mode is "owner"

  Scenario: The owner's speech is delivered
    When the owner says "run the tests again" for 3 seconds
    Then "run the tests again" is delivered as usual
    And the transcript marks "run the tests again" as "owner"

  Scenario: Someone else's speech is never transcribed
    When someone else says "buy now while stocks last" for 4 seconds
    Then nothing was transcribed for someone else
    And nothing is delivered
    And the transcript is empty

  Scenario: A short reply right after the owner is delivered unverified
    When the owner says "shall I push it" for 3 seconds
    And the owner says "yes" for 0.5 seconds after a pause of 1 seconds
    Then "shall I push it" is delivered as usual
    And "yes" is delivered marked unverified

  Scenario: A short phrase with no owner speech just before it is dropped
    When the owner says "yes" for 0.5 seconds
    Then nothing is delivered

  Scenario: Too long a pause ends the follow-on window
    When the owner says "shall I push it" for 3 seconds
    And the owner says "yes" for 0.5 seconds after a pause of 6 seconds
    Then "yes" is not delivered

  Scenario: Someone else speaking ends the follow-on window
    When the owner says "shall I push it" for 3 seconds
    And someone else says "no way" for 2 seconds
    And the owner says "yes" for 0.5 seconds
    Then "yes" is not delivered

  Scenario: Switching microphones ends the follow-on window
    Given the owner has a calibrated voiceprint for "Logitech BRIO"
    When the owner says "shall I push it" for 3 seconds
    And the listener switches to "Logitech BRIO"
    And the owner says "yes" for 0.5 seconds
    Then "yes" is not delivered

  Scenario: A spoken reply ends the follow-on window
    When the owner says "shall I push it" for 3 seconds
    And a spoken reply plays
    And the owner says "yes" for 0.5 seconds
    Then "yes" is not delivered

  Scenario: A microphone with no thresholds is not filtered, and the user is told once
    Given the owner has an uncalibrated voiceprint for "AirPods"
    When someone else says "first" for 3 seconds
    And someone else says "second" for 3 seconds
    Then "first" is delivered as usual
    And "second" is delivered as usual
    And the user is told once that owner-only listening is inactive

  Scenario: A damaged voiceprint is never taken for the owner, and the user is told once
    Given the voiceprint file is damaged
    When someone else says "first" for 3 seconds
    And someone else says "second" for 3 seconds
    Then "first" is delivered as usual
    And the transcript marks "first" as "unknown"
    And the user is told once that owner-only listening is inactive
