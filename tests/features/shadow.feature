Feature: Shadow mode
  Before owner-only listening filters anything, it runs in the shadow: every
  segment is checked against the owner's voiceprint and the verdict is
  written to the private transcript, but what reaches the model is exactly
  what it would have been without the check. That shows, on real days, what
  owner-only listening would have dropped.

  Background:
    Given the owner has a calibrated voiceprint for "AirPods"
    And the listener hears through "AirPods"

  Scenario: Shadow mode changes nothing the model sees
    Given speaker mode is "shadow"
    When someone else says "turn it up" for 3 seconds
    Then "turn it up" is delivered as usual
    And the transcript marks "turn it up" as "other"

  Scenario: The owner is recognised in the shadow
    Given speaker mode is "shadow"
    When the owner says "run the tests" for 3 seconds
    Then "run the tests" is delivered as usual
    And the transcript marks "run the tests" as "owner"

  Scenario: The log records verdicts, never words or scores
    Given speaker mode is "shadow"
    When the owner says "my secret plan" for 3 seconds
    Then the log mentions the verdict "owner"
    And the log does not contain "secret"
    And the log does not contain a score

  Scenario: With speaker mode off nothing is checked
    Given speaker mode is "off"
    When someone else says "turn it up" for 3 seconds
    Then "turn it up" is delivered as usual
    And the transcript has no verdict for "turn it up"

  Scenario: Without a voiceprint the shadow says so once and changes nothing
    Given there is no voiceprint
    And speaker mode is "shadow"
    When someone else says "first" for 3 seconds
    And someone else says "second" for 3 seconds
    Then "first" is delivered as usual
    And "second" is delivered as usual
    And the log says once that there is no voiceprint

  Scenario: Speaker mode can be changed while the listener runs
    Given speaker mode is "off"
    When the user runs "ears speaker shadow"
    And the listener reads its controls
    And someone else says "turn it up" for 3 seconds
    Then the transcript marks "turn it up" as "other"
