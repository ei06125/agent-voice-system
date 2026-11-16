Feature: Calibrating a microphone
  Owner-only listening filters a microphone only once it has thresholds.
  The owner sets them from recordings: held-out clips of their own speech
  and clips of other people, made on that microphone. The command reports
  how well the two separate, and saves thresholds only when asked to.

  Background:
    Given the owner has enrolled "AirPods"

  Scenario: Calibrating from recordings, then saving
    When the owner calibrates "AirPods" from 6 clips of their speech and 6 clips of other voices, saving
    Then "AirPods" is calibrated
    And the report gives the equal error rate and a bound on false accepts

  Scenario: A dry run saves nothing
    When the owner calibrates "AirPods" from 6 clips of their speech and 6 clips of other voices
    Then "AirPods" is not calibrated
    And the report says the thresholds were not saved

  Scenario: Too few recordings
    When the owner calibrates "AirPods" from 1 clips of their speech and 1 clips of other voices, saving
    Then the command fails and says how many trials it needs
    And "AirPods" is not calibrated

  Scenario: A microphone that was never enrolled cannot be calibrated
    When the owner calibrates "Logitech BRIO" from 6 clips of their speech and 6 clips of other voices, saving
    Then the command fails and says to enroll first
